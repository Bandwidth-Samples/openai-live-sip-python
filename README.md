# Bandwidth + OpenAI Live SIP Integration - Python

## Table of Contents

* [Description](#description)
* [Pre-Requisites](#pre-requisites)
* [Environmental Variables](#environmental-variables)
* [Running the Application](#running-the-application)
* [Callback URLs](#callback-urls)
  * [Ngrok](#ngrok)

## Description

This is a sample application that demonstrates how to use Bandwidth's Programmable Voice API with OpenAI's GPT-Live-1 model via the Live API SIP interface to create a real-time AI-powered voice assistant. Unlike the WebSocket integration, the Live SIP integration uses OpenAI's SIP Connector to handle media directly — your application only needs to handle webhooks and send commands via the sideband.

## Pre-Requisites

In order to use this integration you need:

- A Bandwidth Universal Platform account with a Voice Configuration Package and SIP Connector enabled
- Your OpenAI [Project ID](https://help.openai.com/en/articles/9186755-managing-projects-in-the-api-platform) — used to configure your Bandwidth trunk destination
- Your OpenAI [API Key](https://platform.openai.com/api-keys)
- A publicly accessible server to host your webhook application (e.g., using [ngrok](https://ngrok.com/))
- [Docker](https://www.docker.com/) (optional)

Your Bandwidth trunk must point to `sip:$PROJECT_ID@sip.api.openai.com;transport=tls` as the termination destination.

## Environmental Variables

The sample app uses the below environmental variables.

```sh
OPENAI_API_KEY   # Your OpenAI API Key (must have access to the Live API)
REFER_TO         # The phone number to transfer calls to (E.164 format, e.g. +19195554321)
LOG_LEVEL        # The logging level for the application (e.g. INFO, DEBUG)
LOCAL_PORT       # (optional) The local port for the application (default: 3000)
```

Create a `.env` file in the root of the project:

```sh
OPENAI_API_KEY="your_openai_api_key_here"
REFER_TO="+19195554321"
LOG_LEVEL="INFO"
LOCAL_PORT=3000
```

## Running the Application

This application is built using Python 3.13. You can use pip to install the required packages, or Docker Compose to run the application.

```sh
# Using Docker Compose
docker compose up --build
```

```sh
# Using Python
python -m venv .venv
source .venv/bin/activate
cd app
pip install -r requirements.txt
python main.py
```

A successful startup will log:

```sh
INFO:     Uvicorn running on http://0.0.0.0:3000 (Press CTRL+C to quit)
INFO:     Application startup complete.
```

## Callback URLs

Below are the callback paths exposed by this application:

* `/health`
* `/webhooks/openai/live/transport/inbound` — receives `live.transport.incoming` events from OpenAI

### Ngrok

A simple way to set up a local callback URL for testing is to use the free tool [ngrok](https://ngrok.com/).

After you have downloaded and installed `ngrok` run the following command to open a public tunnel to your port (`$LOCAL_PORT`)

```sh
ngrok http $LOCAL_PORT
```

You can view your public URL at `http://127.0.0.1:4040` after ngrok is running.

Configure your OpenAI project's webhook URL to `https://<your-ngrok-url>/webhooks/openai/live/transport/inbound`.
