page_font_style = """
<style>
    @import url('https://fonts.googleapis.com/css2?family=Roboto+Mono:wght@300&display=swap');
    
    * {
    font-family: 'Roboto Mono', monospace;
    }
</style>
"""

button_style = """
<style>
/* Apply the custom styles to Streamlit button */
.stButton > button {
    text-align: center; /* Center button text */
    font-size: 10px; /* Set font size here */
    border: none; /* No border */
    border-radius: 20px; /* Rounded corners */
    background-color: #67748E;
    color: ##3C414A;
    padding: 10px 10px; /* Some padding */
    box-shadow: 0 4px 8px rgba(0,0,0,0.2); /* Box shadow */
}

.stButton > button[kind="primary"] {
    border: 3px solid #DCFF94; /* Red border */
}

.stButton > button:disabled {
    background-color: #636B7D;
}

.stButton > button:hover {
    background-color: #73678F;
    color: #B6E359;
}
</style>
"""
