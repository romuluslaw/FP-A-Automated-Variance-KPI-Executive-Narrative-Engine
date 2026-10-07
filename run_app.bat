@echo off
REM Double-click to open the dashboard (Windows). Requires: pip install -r requirements.txt
cd /d "%~dp0"
if not exist data\input\samples\day3_trial_balance.xlsx python data\generate_mock_data.py
python -m streamlit run app.py
pause
