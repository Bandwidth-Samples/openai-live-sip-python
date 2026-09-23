import asyncio
import http
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env", override=True)

from openai import AsyncOpenAI
from openai.resources.live.sideband import AsyncSidebandConnection
from typing import Optional
from fastapi import FastAPI, Response
from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.text import Text
import uvicorn

from models.live_transport_incoming import LiveTransportIncoming

# Load environment variables
console = Console()
try:
    OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
    REFER_TO = os.environ["REFER_TO"]
    LOG_LEVEL = os.environ["LOG_LEVEL"].upper()
    LOCAL_PORT = int(os.environ.get("LOCAL_PORT", 3000))
except KeyError as e:
    msg = Text(" Missing environment variables! ", style="bold white on red")
    details = f"Required key not set: [yellow]{e.args[0]}[/yellow]\n\n"
    details += "Make sure the following variables are defined:\n"
    details += "[cyan]OPENAI_API_KEY, REFER_TO, LOG_LEVEL[/cyan]"
    console.print(Panel(details, title=msg, expand=False, border_style="red"))
    sys.exit(1)

# Configure Logger
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(levelname)s %(asctime)s: %(message)s",
    datefmt="[%X]",
)
for name in ["websockets", "asyncio", "urllib3", "uvicorn", "fastapi", "openai", "httpx", "httpcore2", "httpcore"]:
    logging.getLogger(name).setLevel(logging.INFO)
logger = logging.getLogger(__name__)

# OpenAI Live Client
openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)

# OpenAI Live API Settings
OPENAI_LIVE_MODEL = "gpt-live-1"
OPENAI_RESPONSES_MODEL = "gpt-5.6-terra"
AGENT_VOICE = "alloy"
with open("sample-prompt.md", "r") as file:
    AGENT_PROMPT = file.read()

# Frontend instructions for the Live model, appended to the shared persona prompt.
# The Live model — not the client — decides when a turn is handed to the Responses
# backend, and it only delegates when the Live instructions tell it to. Without these
# conditions it answers every turn conversationally and the backend tools (`refer`,
# `web_search`) are never reached. See
# https://developers.openai.com/api/docs/guides/live-prompting#delegation
DELEGATION_INSTRUCTIONS = """

## Delegating to the backend

A backend assistant handles every task that needs a tool. Its capabilities are:

* Transfers — moving the caller to a live human agent.
* Lookups — factual questions such as prices, news, and availability.

Delegate to the backend when:

* The caller asks to speak to a human, be transferred, get an agent, or talk to a person.
* The caller asks a factual question you cannot answer from this conversation.
* The request needs a backend capability or careful reasoning.

Delegate before giving an answer that depends on backend work. Do not guess the result
while waiting, and never tell the caller a transfer is happening until the backend confirms it.

Do not delegate for greetings, small talk, or answers already given in this conversation.
"""

TOOLS = [
    {
        "type": "function",
        "name": "refer",
        "description": (
            "Transfer the call to a live human agent. "
            "ONLY call this when the caller explicitly says they want to speak to a human, "
            "be transferred, or get an agent. Never use it for questions — use web_search instead."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    {"type": "web_search"},
]

# Initialize FastAPI app
app = FastAPI()

# Active Live SIP session start times keyed by session_id
# ponytail: module-level dict; per-instance storage if multi-worker
session_start_times: dict[str, datetime] = {}


def _print_transcript(speaker: str, text: str) -> None:
    """
    Print a complete transcript utterance with rich styling.
    :param speaker: 'input' (caller) or 'output' (AI)
    :param text: The complete utterance text
    :return: None
    """
    if speaker == "input":
        console.print(f"[bold cyan]  Caller[/bold cyan] │ {text}", highlight=False)
    else:
        console.print(f"[bold magenta]   Agent[/bold magenta] │ {text}", highlight=False)


def _print_call_start(session_id: str) -> None:
    """Print a call start panel with session ID and timestamp."""
    now = datetime.now().strftime("%H:%M:%S")
    console.print(Panel(
        f"[bold white]{session_id}[/bold white]   [dim]{now}[/dim]",
        title="[bold blue] Inbound Call [/bold blue]",
        border_style="blue",
    ))


def _print_call_end(session_id: str) -> None:
    """Print a call end panel with duration."""
    start = session_start_times.pop(session_id, None)
    if start:
        elapsed = int((datetime.now() - start).total_seconds())
        duration = f"{elapsed // 60}:{elapsed % 60:02d}"
        body = Text.assemble(
            ("Duration  ", "dim"),
            (duration, "bold white"),
        )
    else:
        body = Text("Session closed", style="dim")
    console.print(Panel(body, title="[bold red] Call Ended [/bold red]", border_style="dim red"))


async def sideband_task(session_id: str, sip_host: Optional[str] = None) -> None:
    """
    Connect to the sideband for an active Live SIP session and handle tool calls.
    When the Responses backend invokes the `refer` function, transfer the call.

    Audio and turn taking are owned by the Live model, so `response.create` is only
    sent twice: once to open the call with a backend-driven greeting, and again after
    each tool result to continue the delegated response. Every other delegation is
    started by the Live model itself based on DELEGATION_INSTRUCTIONS.

    :param session_id: The Live session ID to attach to
    :param sip_host: Host from the inbound Contact header, used to build the refer URI
    :return: None
    """
    buf: dict[str, str] = {"input": "", "output": ""}
    last_speaker: str | None = None

    def flush(speaker: str) -> None:
        text = buf[speaker].strip()
        if text:
            _print_transcript(speaker, text)
            buf[speaker] = ""

    try:
        async with openai_client.live.sideband.connect(session_id=session_id) as connection:
            await connection.response.create()
            console.print(Rule("[dim green]AI ready[/dim green]"))
            async for event in connection:
                match event.type:
                    case "session.input_transcript.delta":
                        delta = getattr(event, "delta", "")
                        if last_speaker == "output":
                            flush("output")
                        buf["input"] += delta
                        last_speaker = "input"
                    case "session.output_transcript.delta":
                        delta = getattr(event, "delta", "")
                        if last_speaker == "input":
                            flush("input")
                        buf["output"] += delta
                        last_speaker = "output"
                        if buf["output"].rstrip().endswith((".", "!", "?", "…")):
                            flush("output")
                    case "response.event":
                        await handle_response_event(event, connection, session_id, sip_host)
                    case "session.closed":
                        if last_speaker:
                            flush(last_speaker)
                        _print_call_end(session_id)
                        return
                    case "error":
                        logger.error(f"Sideband error [{session_id}]: {getattr(event, 'error', event)}")
                    case "session.usage.updated" | "session.input_audio.append" | "session.output_audio.delta" | "session.started":
                        pass
                    case "session.delegation.created":
                        delegation = getattr(event, "delegation", None)
                        logger.debug(
                            f"Delegation created: id={getattr(delegation, 'id', '?')} "
                            f"target={getattr(delegation, 'target', '?')}"
                        )
                    case _:
                        logger.debug(f"Unhandled OpenAI event: {event.type}")
    except Exception as e:
        if str(e):
            logger.error(f"Sideband connection error [{session_id}]: {e}")


async def handle_response_event(
    event, connection: AsyncSidebandConnection, session_id: str, sip_host: Optional[str]
) -> None:
    """
    Handle response.event messages forwarded from the Responses delegation backend.
    Dispatches on the nested event type; executes tool calls when they complete.

    :param event: The response.event from the Live sideband
    :param connection: The Live sideband connection
    :param session_id: The Live session ID
    :param sip_host: Host from the inbound Contact header, used to build the refer URI
    :return: None
    """
    backend_event: dict = event.event
    event_type = backend_event.get("type")
    if event_type == "response.output_item.done":
        item = backend_event.get("item", {})
        item_type = item.get("type")
        name = item.get("name")
        logger.debug(f"response.output_item.done: item_type={item_type}" + (f" name={name}" if name else ""))
        if item_type == "function_call":
            await handle_tool_call(item, connection, session_id, sip_host)


async def handle_tool_call(
    item: dict, connection: AsyncSidebandConnection, session_id: str, sip_host: Optional[str]
) -> None:
    """
    Handle tool calls from the Responses delegation backend.
    Always sends a function_call_output result back so the AI can respond.

    :param item: The function_call item from the Responses backend
    :param connection: The Live sideband connection
    :param session_id: The Live session ID
    :param sip_host: Host from the inbound Contact header, used to build the refer URI
    :return: None
    """
    function_name = item.get("name")
    tool_call_id = item.get("call_id") or item.get("id")

    result: str
    match function_name:
        case "refer":
            target_uri = f"sip:{REFER_TO}@{sip_host}" if sip_host else f"tel:{REFER_TO}"
            logger.debug(f"sip_host={sip_host!r} target_uri={target_uri!r}")
            logger.info(f"Referring session {session_id} to {target_uri}")
            try:
                await openai_client.live.sessions.refer(session_id, target_uri=target_uri)
                logger.info("Refer accepted by OpenAI — waiting for session.closed")
                result = "success"
            except Exception as e:
                logger.error(f"Error referring call: {e}")
                result = f"error: {e}"
        case _:
            logger.warning(f"Unhandled function call: {function_name}")
            result = f"error: unknown function {function_name}"

    await connection.response.item.create(item={
        "type": "function_call_output",
        "call_id": tool_call_id,
        "output": result,
    })
    await connection.response.create()


@app.get("/health", status_code=http.HTTPStatus.NO_CONTENT)
def health():
    """
    Health check endpoint.
    :return: None
    """
    return


@app.post("/webhooks/openai/live/transport/inbound", status_code=http.HTTPStatus.OK)
async def handle_inbound_call(event: LiveTransportIncoming) -> Response:
    """
    Handle the live.transport.incoming webhook from OpenAI.
    Accepts the SIP session, configures the AI agent, and starts the sideband listener.
    Must return 200 OK before OpenAI connects the call.
    """
    if event.type == "live.transport.incoming" and event.data.type == "sip":
        session_id = event.data.session_id
        sip_host = event.data.get_sip_host()
        session_start_times[session_id] = datetime.now()
        _print_call_start(session_id)
        logger.info(f"Received inbound call event for session ID: {session_id}")
        logger.debug(f"Webhook data: {event.data.model_dump()}")

        await openai_client.live.sessions.accept(
            session_id=session_id,
            session={
                "type": "live",
                "model": OPENAI_LIVE_MODEL,
                "instructions": AGENT_PROMPT + DELEGATION_INSTRUCTIONS,
                "audio": {"output": {"voice": AGENT_VOICE}},
                "delegation": {
                    "type": "responses",
                    "responses": {
                        "model": OPENAI_RESPONSES_MODEL,
                        "instructions": AGENT_PROMPT,
                        "tools": TOOLS,
                        "tool_choice": "auto",
                    },
                },
            },
        )
        asyncio.create_task(sideband_task(session_id, sip_host))
    else:
        logger.debug(f"Ignoring event type: {event.type}")

    return Response()


def start_server(port: int) -> None:
    """
    Start the FastAPI server.

    :param port: The port to run the server on
    :return: None
    """
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        log_level="info",
        reload=True,
    )


if __name__ == "__main__":
    start_server(LOCAL_PORT)
