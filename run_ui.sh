source "$(poetry env info --path)/bin/activate"
python skyvern/analytics.py skyvern-oss-run-ui
streamlit run streamlit_app/visualizer/streamlit.py -- $@
