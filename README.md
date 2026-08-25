# Reggeli Becslés

An automated Python tool that downloads daily gas consumption files from Google Drive, aggregates multi-source measurement data, calculates portfolio positions, and posts real-time status updates and daily position summaries to Google Chat.

## Features

* **Automated File Processing**: Retrieves daily CSV and Excel data files (FŐGÁZ, ÉD, TIGÁZ) from Google Drive.
* **Smart Retry Mechanism**: Automatically retries file downloads if files are missing, sending dynamic status alerts to Google Chat.
* **Database Integration**: Connects via SQLAlchemy to pull POD mappings, conversion factors, and nominated gas volumes.
* **Fallback Handling**: Handles missing sources by substituting nominated values.
* **Position Summary**: Generates daily estimates (PF, Balance, Short/Long positions) and posts summaries directly to Google Chat.

## Requirements

* Python 3.9+
* Required packages: `pandas`, `numpy`, `openpyxl`, `sqlalchemy`
* Internal libraries: `wattler_tools` (`wdb`, `wattler_drive`, `wattler_chat`)

## Configuration

Key settings can be updated directly at the top of the main script:

* `RETRY_DELAY_SECONDS`: Wait duration between retry attempts (e.g., `600` for 10 minutes).
* `MAX_RETRIES`: Number of retry attempts before executing fallbacks.
* `SHARED_DRIVE_FOLDER_ID`: Target Google Drive folder ID containing the raw files. (https://drive.google.com/drive/folders/1XHfnTEHt3GKgS8S-R2f0wcSpb8pP__yG)
* `CHAT_ID`: Target Google Chat space identifier.

## Usage

Run the main pipeline:

```bash
python main.py
