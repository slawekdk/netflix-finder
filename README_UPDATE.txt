Netflix Finder by Slawek — v1.8.0 update

Replace these files in the GitHub repository:
- app/main.py
- app/static/app.js
- app/static/style.css

Do NOT remove manifest.json, sw.js or the existing icons.

Main fixes:
1. Frontend and backend API parameter names are compatible.
2. /search now returns criteria, so Search no longer crashes on d.criteria.sort.
3. Detail view uses the actual backend fields (poster_path, runtime, netflix_available).
4. /health reports backend version 1.8.0.
5. /top10 remains available.
6. Mobile hero/title area is pushed lower to avoid the iPhone safe-area/status-bar overlap.
7. Existing PWA files are intentionally left untouched.

After committing the three files, wait for Render to redeploy. Then test:
https://netflix-finder.onrender.com/health
Expected version: 1.8.0
