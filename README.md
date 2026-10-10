# Telegram Python Hosting Bot

A Telegram bot for uploading Python/JavaScript files and ZIP archives, running them in a managed environment, viewing logs, and controlling active processes.

## Features
- Upload Python scripts
- Upload JavaScript files
- Upload ZIP archives and auto-extract them
- Auto-detect main script file inside extracted archives
- Run, restart, and stop uploaded processes
- View logs
- SQLite-based metadata storage
- Admin panel for managing users/files
- System health monitoring (CPU/RAM)

## Project structure
- `bot.py` — main Telegram bot code
- `requirements.txt` — Python dependencies
- `.env.example` — example environment variables
- `.gitignore` — ignore generated files
- `README.md` — setup and usage guide

## Prerequisites
- Python 3.10+
- Telegram bot token from BotFather
- A Linux/macOS/Windows environment with Python access

## Setup

1. Clone the project.
2. Create a virtual environment.
3. Install the dependencies.
4. Copy `.env.example` to `.env` and configure your bot token.
5. Run the bot.

### Linux/macOS
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python bot.py
```

### Windows
```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
python bot.py
```

## Environment variables
Create a `.env` file with:

```env
BOT_TOKEN=YOUR_TELEGRAM_BOT_TOKEN
ADMIN_IDS=8556512504
CPU_THRESHOLD=95
MEMORY_THRESHOLD=95
MAX_RUNNING_PROCESSES=15
MAX_FILES_PER_USER=5
```

## Usage
- Start the bot with `/start`
- Upload a .py, .js, or .zip file
- Use the inline buttons to run, stop, restart, or delete files
- Access admin panel if your Telegram ID is in `ADMIN_IDS`

## Notes
- `BOT_TOKEN` must be obtained from Telegram BotFather.
- `ADMIN_IDS` is a comma-separated list of Telegram user IDs.
- For production, do not commit `.env` files to Git.
- Use a proper process manager or service manager in production.

## Security
- Never hardcode your Telegram token in source code.
- Store tokens in `.env` and keep `.env` out of Git.
- Secure the server environment and restrict public access.

## License
MIT
