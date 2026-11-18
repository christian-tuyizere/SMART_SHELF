# SmartShelf on Render
1. Upload these files to a NEW GitHub repository (never upload a real .env).
2. render.com > New > Blueprint > pick the repo. Fill the 4 values it asks for:
   APP_PASSWORD (any password, share it with your lecturer), YCLOUD_API_KEY (your NEW key),
   YCLOUD_FROM (+250780814741), SHOP_NAME.
3. Wait for "Live". Open the https://....onrender.com link, log in (any username, the password you chose).
4. Keep it awake and make due-date reminders run: free pinger (cron-job.org) calling https://YOUR-LINK/healthz every 10 minutes.
Free plan: saved data resets on restart (sample data is re-added). WhatsApp delivery needs your YCloud account approved.
