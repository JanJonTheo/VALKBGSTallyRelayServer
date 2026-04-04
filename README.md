# VALK BGS-Tally Relay Server

![Version](https://img.shields.io/badge/version-1.0.0-blue)
![Status](https://img.shields.io/badge/status-stable-success)
![License](https://img.shields.io/badge/license-MIT-green)
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

Special Thanks to TobyToolBag and NetTrap for the idea and the input for this project.
