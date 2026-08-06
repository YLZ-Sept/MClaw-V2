# Getting Started

This guide will help you get Mclaw up and running quickly.

## Prerequisites

Before you begin, ensure you have:

- **Python 3.11+** installed
- An **Anthropic API key** ([get one here](https://console.anthropic.com/))
- **Git** for cloning the repository

## Installation

### Option 1: Install from PyPI (Recommended)

```bash
# Create and activate virtual environment
python -m venv venv
source venv/bin/activate  # Linux/macOS
# or .\venv\Scripts\activate  # Windows

# Install Mclaw (core)
pip install mclaw

# Optional features
pip install "mclaw[all]"      # install all optional features
# pip install "mclaw[windows]"  # Windows desktop automation
# pip install "mclaw[feishu]"   # Feishu (Lark)

# Run setup wizard
mclaw init
```

### Option 2: One-click install script (PyPI)

Linux/macOS:

```bash
curl -fsSL https://raw.githubusercontent.com/mclaw/mclaw/main/scripts/quickstart.sh | bash
```

Windows (PowerShell):

```powershell
irm https://raw.githubusercontent.com/mclaw/mclaw/main/scripts/quickstart.ps1 | iex
```

To install extras / use a mirror, download and run with parameters (recommended):

```bash
curl -fsSL -o quickstart.sh https://raw.githubusercontent.com/mclaw/mclaw/main/scripts/quickstart.sh
bash quickstart.sh --extras all --index-url https://pypi.tuna.tsinghua.edu.cn/simple
```

```powershell
irm https://raw.githubusercontent.com/mclaw/mclaw/main/scripts/quickstart.ps1 -OutFile quickstart.ps1
.\quickstart.ps1 -Extras all -IndexUrl https://pypi.tuna.tsinghua.edu.cn/simple
```

### Option 3: Install from Source (Development)

```bash
git clone https://github.com/mclaw/mclaw.git
cd mclaw
python -m venv venv
source venv/bin/activate  # Windows: .\venv\Scripts\activate
pip install -e ".[all,dev]"
mclaw init
```

## Configuration

### 1. Create Environment File

```bash
cp examples/.env.example .env
```

### 2. Add Your API Key

Edit `.env` and set your Anthropic API key:

```bash
ANTHROPIC_API_KEY=sk-your-api-key-here
```

### 3. Optional Settings

```bash
# Custom API endpoint (useful for proxies)
ANTHROPIC_BASE_URL=https://api.anthropic.com

# Model selection
DEFAULT_MODEL=claude-sonnet-4-20250514

# Agent behavior
MAX_ITERATIONS=100
AUTO_CONFIRM=false
```

## Your First Run

### Start the CLI

```bash
mclaw
```

You should see:

```
╭─────────────────────────────────────────╮
│           Mclaw v0.5.9                │
│   A Self-Evolving AI Agent              │
╰─────────────────────────────────────────╯

You> 
```

### Try a Simple Task

```
You> Hello, what can you do?
```

Mclaw will introduce itself and explain its capabilities.

### Try a Complex Task

```
You> Create a Python script that calculates prime numbers up to 100
```

Watch as Mclaw:
1. Analyzes the task
2. Writes the code
3. Tests it
4. Reports the results

## Common Commands

| Command | Description |
|---------|-------------|
| `mclaw` | Start interactive mode |
| `mclaw run "task"` | Execute a single task |
| `mclaw status` | Show agent status |
| `mclaw selfcheck` | Run self-diagnostics |
| `mclaw --help` | Show all commands |

## Next Steps

- [Architecture Overview](architecture.md) - Understand how Mclaw works
- [Configuration Guide](configuration.md) - All configuration options
- [Skills System](skills.md) - Create custom skills
- [IM Channels](im-channels.md) - Set up Telegram, etc.

## Troubleshooting

### "API key not found"

Ensure your `.env` file exists and contains `ANTHROPIC_API_KEY`.

### "Connection timeout"

Check your network connection. If in China, consider using a proxy:

```bash
ANTHROPIC_BASE_URL=https://your-proxy-url
```

### "Python version error"

Mclaw requires Python 3.11+. Check your version:

```bash
python --version
```

### Need More Help?

- Check [GitHub Issues](https://github.com/mclaw/mclaw/issues)
- Join [GitHub Discussions](https://github.com/mclaw/mclaw/discussions)
