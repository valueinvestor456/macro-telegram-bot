# FINAL SETUP - One Command to Run Everything

## 1. Install Python (MUST DO FIRST)
Go to: https://www.python.org/downloads/

**Click the big yellow "Download Python 3.12"**

Run the installer:
- ✅ CHECK THE BOX: "Add Python to PATH" (IMPORTANT!)
- Click "Install Now"
- Wait for "Setup was successful"
- **Close the installer**

## 2. Verify Python Works
Open PowerShell and type:
```powershell
python --version
```

You should see: `Python 3.12.x`

If you see "not recognized", Python didn't install correctly. Try again and make sure to check "Add Python to PATH".

## 3. Run This ONE Command
Open PowerShell, go to your bot folder, and copy-paste this:

```powershell
cd d:\VSCODE\macro-telegram-bot
python -m pip install --upgrade pip -q
pip install -q yfinance requests beautifulsoup4 schedule pytz
python main.py
```

## 4. Test It
1. Keep the terminal open
2. Send `/usd` in Telegram
3. **You should get a reply in 5 seconds**

## That's it!
If it works, your bot will keep running as long as this terminal is open.

---

## Troubleshooting
- **"python not found"** → Python not installed. Go back to Step 1 and check "Add Python to PATH"
- **"ModuleNotFoundError"** → Run the pip install command again
- **No reply in Telegram** → Screenshot the terminal and show me the error
