# VALK BGS-Tally Relay Server

![Version](https://img.shields.io/badge/version-0.3.0-blue)
![Status](https://img.shields.io/badge/status-stable-success)
![License](https://img.shields.io/badge/license-GNU-green)
![Platform](https://img.shields.io/badge/platform-Windows-lightgrey)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![Framework](https://img.shields.io/badge/PySide6-GUI-orange)

**Author:** CMDR JanJonTheo

---

## 🚀 Overview
![Screenshot 2026-04-04 105757.png](screenshots/Screenshot%202026-04-04%20105757.png)
The **VALK BGS-Tally Relay Server** is a lightweight desktop application designed to act as a local relay between BGS-Tally clients and one or more remote servers.

It receives incoming **Events** and **Activities** via HTTP and forwards them **1:1** to configured target servers — without storing any data locally.

This makes it ideal for:

* Multi-server synchronization
* Redundant data forwarding
* Local aggregation points
* Secure API isolation layers

---

## ⚙️ Features

* 🛰️ Local HTTP relay server (`/events`, `/activities`)
* 🔁 Forwarding to multiple target servers
* 🔐 Local API key protection (`localapikey`)
* 🧠 No database – fully stateless operation
* 🖥️ PySide6 desktop UI
* 📊 Live health dashboard:

  * Requests/sec & Requests/min
  * Total events & activities received
  * Forward success / error tracking
  * Last error & status
  * Uptime monitoring
* 📦 Tray mode (runs in background)
* 🔄 Auto-start with Windows
* 📝 Rotating logfile (`relay_server.log`, max 1024 KB)
* 🎨 VALK branded UI with custom icon

---

## 🧱 Architecture

```
Client (BGS-Tally)
        │
        ▼
VALK Relay Server (local)
        │
        ├──► Target Server A
        ├──► Target Server B
        └──► Target Server C
```

* Incoming requests are **not modified**
* Payloads are forwarded exactly as received
* No persistence layer is used

---

## 🔧 Requirements

* Python 3.10+
* Windows (recommended for auto-start feature)

Install dependencies:

```bash
pip install PySide6 flask requests
```

---

## ▶️ How to Run

```bash
python relay_pyside6.py
```

The application will:

* Start the local relay server
* Open the UI
* Begin listening for incoming requests

---

## ⚙️ Configuration

Targets can be configured directly in the UI.

Each target includes:

* Server URL
* API Key
* API Version
* Timeout
* Enable/Disable for Events / Activities

---

## 🎯 Cmdr-based Filtering

The relay server supports **optional per-target filtering by CMDR name**.

This allows you to forward only specific commanders to selected target servers.

### ⚙️ Configuration

Each relay target can define a list of allowed CMDR names:

```json
{
  "name": "Server A",
  "base_url": "http://127.0.0.1:5000",
  "cmdr_filters": ["JanJonTheo", "Bulli75"]
}
```

### 🧠 Behavior

* If `cmdr_filters` is **set and not empty**:

  * Only matching CMDRs will be forwarded
* If `cmdr_filters` is **empty or not set**:

  * All data will be forwarded (no filtering)

### 📥 Events (`/events`)

* Incoming events are filtered **per entry**
* Only matching events are forwarded
* Non-matching events are discarded for that target

### 📥 Activities (`/activities`)

* The `cmdr` field is evaluated
* The entire request is forwarded **only if it matches**

---

## 📝 Filter Logging

All filtering actions are fully logged for transparency and debugging.

### Example Logs

```text
FILTER POST /events -> Server A: 2/10 event(s) matched [JanJonTheo, Bulli75]
POST /events -> Server A [200] http://127.0.0.1:5000/events (forwarded 2 filtered event(s))
SKIP POST /events -> Server A: all events filtered out [JanJonTheo, Bulli75]

FILTER PUT /activities -> Server A: activity matched (CMDR: JanJonTheo, filter: [JanJonTheo, Bulli75])
SKIP PUT /activities -> Server A: activity filtered out (CMDR: RandomCmdr, filter: [JanJonTheo, Bulli75])
```

---

## 🎯 Default Objectives Server

The relay supports a **dedicated default server for the `/objectives` endpoint**.

Unlike `/events` and `/activities`, the `/objectives` endpoint can only be handled by **one single target server**.

### ⚙️ Configuration

Each target can optionally be marked as the default objectives server:

```json
{
  "name": "Server A",
  "base_url": "http://127.0.0.1:5000",
  "default_objectives": true
}
```

> ⚠️ Only **one server** should have `default_objectives = true`

---

### 🧠 Behavior

* `/events` → forwarded to **multiple targets** (fan-out)
* `/activities` → forwarded to **multiple targets** (fan-out)
* `/objectives` → forwarded to **exactly one default server**

---

### 📡 Supported Operations

The relay transparently forwards the following endpoints:

* `GET /objectives`
* `POST /objectives`
* `DELETE /objectives/<id>`

All requests are:

* forwarded **unchanged**
* executed only against the default server
* responses are passed back to the client

---

### ❌ Error Handling

If no default server is configured:

* `/objectives` requests will return an error
* a log entry will be generated

---

### 📝 Logging

Example:

```text
GET /objectives -> Default Server A [200]
POST /objectives -> Default Server A [201]
ERROR /objectives -> No default server configured
```

---

## 📊 Per-Target Statistics (UI)

Each relay target row now includes **per-endpoint statistics** directly in the table.

### Columns

* **Events Calls**
* **Activities Calls**
* **Objectives Calls**

### Format

```
F:<forwarded> | X:<filtered>
```

* `F` = number of successfully forwarded calls
* `X` = number of filtered / skipped calls

### Examples

```
Events Calls:     F:12 | X:3
Activities Calls: F:5  | X:1
Objectives Calls: F:2  | X:0
```

### Behavior

* Counters are maintained **per target server**
* Updated in real-time as requests are processed
* Reflect both **CMDR filtering** and **routing decisions**
* Objectives counters only apply to the configured default server

This provides immediate visibility into:

* how much data is forwarded vs. discarded
* which targets are actively receiving traffic
* effectiveness of filtering rules

---

## 🧪 Health Monitoring

The integrated dashboard provides real-time insights:

* Request throughput
* Forwarding success rate
* Error tracking
* System uptime

This allows quick detection of:

* Connectivity issues
* Misconfigured endpoints
* API authentication problems

---

## 🔄 Tray Mode

* The application can run fully in the background
* Closing the window minimizes it to the system tray
* Double-click the tray icon to restore

---

## 🚀 Windows Auto-Start

The application can be configured to start automatically with Windows.

This ensures the relay is always active without manual intervention.

---

## 🖥️ UI Logging Controls

The application includes built-in logging controls directly in the user interface.

### Log Level Selection

The log level can be changed at runtime:

* `INFO`
* `DEBUG`

This allows you to switch between normal operational logging and more verbose diagnostic output.

### Configuration Persistence

The selected log level is stored in the application configuration and restored on the next startup.

This makes it easy to keep a preferred logging mode for daily use or troubleshooting sessions.

---

## 🔎 Log Viewer Filters

The integrated log viewer supports filtering directly inside the application.

### Available Filters

* **Level filter**

  * `All`
  * `ERROR`
  * `WARNING`
  * `INFO`
  * `DEBUG`

* **Text filter**

  * Filter by server name
  * Filter by endpoint such as `/events`, `/activities`, `/objectives`
  * Filter by log markers such as `FORWARD`, `FILTER`, `OBJECTIVES`, `API_REJECT`

### Examples

* `Server A`
* `/events`
* `OBJECTIVES_ERROR`
* `FORWARD_OK`

This makes it easier to:

* focus on a specific relay target
* inspect only errors or warnings
* troubleshoot objectives routing separately
* isolate filtering behavior for a single CMDR or endpoint

---

## 📄 Logging

All activity is logged to:

```
relay_server.log
```

* Max size: **1024 KB**
* Automatic rotation
* Includes forwarding status and errors

---

## 🔍 Detailed Logging

The relay provides **highly detailed logging** for all incoming API calls, filtering decisions, and forwarding operations.

### 📥 Incoming Requests

Each request logs:

* HTTP method and endpoint
* Remote IP address
* API version
* Content length
* User-Agent

Example:

```text
API_CALL POST /events: accepted | remote=127.0.0.1 apiversion=1.6.0 content_length=842 ua='EDMC BGS-Tally'
```

---

### 📦 Payload Summary

* `/events`:

  * number of events
  * event types
  * CMDR names

* `/activities` / `/objectives`:

  * available keys
  * CMDR name (if present)

Example:

```text
DISPATCH POST /events: target_count=2 | count=5 events=[MarketBuy, MissionCompleted] cmdrs=[JanJonTheo]
```

---

### 🎯 Filtering Logs

* number of matching vs. total events
* skipped calls
* CMDR filter details

Example:

```text
FILTER POST /events -> Server A: 2/10 event(s) matched [JanJonTheo, Bulli75]
SKIP PUT /activities -> Server A: activity filtered out (CMDR: RandomCmdr)
```

---

### 🔁 Forwarding Logs

Each forwarding attempt logs:

* target server name
* target URL
* timeout
* header usage
* response status code
* response size
* execution time

Example:

```text
FORWARD_BEGIN POST /events -> Server A | url=http://127.0.0.1:5000/events | timeout=15s
FORWARD_OK POST /events -> Server A [200] | elapsed_ms=42 | response_bytes=39
```

---

### 🎯 Objectives Logging

Dedicated logging for `/objectives` routing:

```text
OBJECTIVES_BEGIN GET /objectives -> Default Server A
OBJECTIVES_OK GET /objectives -> Default Server A [200]
OBJECTIVES_ERROR POST /objectives -> Default Server A: timeout
```

---

### ❌ Error Logging

* invalid payloads (`API_REJECT`)
* unauthorized access
* forwarding failures

Example:

```text
API_REJECT POST /events: invalid payload (expected list)
ERROR POST /events -> Server A: Connection timeout
```

---
## 📄 Logging

All activity is logged to:

```
relay_server.log
```

* Max size: **1024 KB**
* Automatic rotation
* Includes forwarding status and errors

---

## 🧩 Use Cases

* Multi-faction data distribution
* Redundant BGS tracking pipelines
* Secure internal relay before exposing public API
* Load distribution across multiple backends

---

## ⚠️ Notes

* This application **does not store any data**
* All forwarding is done in real-time
* If a target server fails, errors are logged but do not block other targets

---

## 🛡️ License

GNU General Public License v3.0

Copyright (c) 2026 CMDR JanJonTheo

This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.

This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.

You should have received a copy of the GNU General Public License along with this program. If not, see https://www.gnu.org/licenses/.

---

## ✨ Disclaimer, Credits, Special Thanks

This project is not affiliated with or endorsed by Frontier Developments Inc., the creators of Elite Dangerous.

Special Thanks to Aussi, TobyToolBag and NetTrap for the idea and the input for this project.

