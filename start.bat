@echo off
color 0A
echo ==========================================
echo    STARTING MASTER 7 BOT VIA PM2
echo ==========================================

:: รัน Uvicorn ผ่าน PM2 โดยชี้ไปที่ venv
pm2 start venv\Scripts\uvicorn.exe --name "master7_bot" -- main:app --host 127.0.0.1 --port 8000

:: บันทึกสถานะ PM2 ให้รันอัตโนมัติเมื่อเปิดเครื่อง (ถ้าตั้งค่า pm2 startup ไว้)
pm2 save

echo.
echo [SUCCESS] Bot is running on port 8000!
pause