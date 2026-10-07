#!/usr/bin/env bash
# Open the dashboard (macOS/Linux). Requires: pip install -r requirements.txt
cd "$(dirname "$0")"
[ -f data/input/samples/day3_trial_balance.xlsx ] || python3 data/generate_mock_data.py
python3 -m streamlit run app.py
