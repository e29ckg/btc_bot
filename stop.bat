@echo off
color 0C
echo ==========================================
echo    STOPPING MASTER 7 BOT
echo ==========================================

pm2 stop master7_bot
pm2 delete master7_bot
pm2 save

echo.
echo [SUCCESS] Bot stopped and removed from PM2!
pause