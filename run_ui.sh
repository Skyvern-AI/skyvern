

source "$(poetry env info --path)/bin/activate"
python scripts/tracking.py skyvern-oss-run-ui
streamlit run streamlit_app/visualizer/streamlit.py -- $@
