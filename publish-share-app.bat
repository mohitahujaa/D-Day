@echo off
rem Publishes the D-Day phone share app (share_app\) to GitHub Pages:
rem   repository  https://github.com/mohitahujaa/dday-share   (public; static page, no secrets)
rem   app address https://mohitahujaa.github.io/dday-share/
rem Needs the GitHub CLI (gh), already signed in. Run it again to publish updates.
setlocal
set REPO=mohitahujaa/dday-share
set SRC=%~dp0share_app
set WORK=%TEMP%\dday-share-repo

where gh >nul 2>nul || (echo GitHub CLI not found: https://cli.github.com/ & pause & exit /b 1)
gh auth status >nul 2>nul || (echo Sign in first: gh auth login & pause & exit /b 1)

if exist "%WORK%" rmdir /s /q "%WORK%"
mkdir "%WORK%"
xcopy /e /y /q "%SRC%\*" "%WORK%\" >nul
type nul > "%WORK%\.nojekyll"
(
  echo # D-Day share
  echo.
  echo Share a message from your Android phone ^(WhatsApp, SMS, email^) into D-Day, a local deadline assistant.
  echo The page adds the text to a "D-Day" list in your own Google Tasks; D-Day on your PC picks it up.
  echo There is no server: this static page talks only to Google, with your own sign-in.
) > "%WORK%\README.md"

cd /d "%WORK%"
git init -q -b main
git add -A
git commit -q -m "Publish D-Day share app"

gh repo view %REPO% >nul 2>nul
if errorlevel 1 (
  gh repo create %REPO% --public --description "Share messages from your phone to D-Day (via your Google Tasks)" --source . --push || (pause & exit /b 1)
) else (
  git remote add origin https://github.com/%REPO%.git
  git push -f -q origin main || (pause & exit /b 1)
)

rem Turn on GitHub Pages (ignored if it's already on).
gh api -X POST repos/%REPO%/pages -f "source[branch]=main" -f "source[path]=/" >nul 2>nul

echo.
echo Published. The app will be live in about a minute at:
echo   https://mohitahujaa.github.io/dday-share/
echo Next: D-Day dashboard -^> Settings -^> Share from phone.
pause
