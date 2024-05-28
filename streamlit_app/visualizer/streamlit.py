import json
import sys

import clipboard
import pandas as pd
import streamlit as st

from skyvern import analytics
from skyvern.forge.sdk.schemas.tasks import ProxyLocation, TaskRequest
from streamlit_app.visualizer import styles
from streamlit_app.visualizer.api import SkyvernClient
from streamlit_app.visualizer.artifact_loader import (
    read_artifact_safe,
    streamlit_content_safe,
    streamlit_show_recording,
)
from streamlit_app.visualizer.repository import TaskRepository
from streamlit_app.visualizer.sample_data import supported_examples

analytics.capture("skyvern-oss-run-ui")

# Streamlit UI Configuration
st.set_page_config(layout="wide")

# Apply styles
st.markdown(styles.page_font_style, unsafe_allow_html=True)
st.markdown(styles.button_style, unsafe_allow_html=True)

tab_name = sys.argv[1] if len(sys.argv) > 1 else ""


# Configuration
def reset_session_state() -> None:
    # Delete all the items in Session state when env or org is changed
    for key in st.session_state.keys():
        del st.session_state[key]


CONFIGS_DICT = st.secrets["skyvern"]["configs"]
if not CONFIGS_DICT:
    raise Exception("No configuration found. Copy the values from 1P and restart the app.")
SETTINGS = {}
for config in CONFIGS_DICT:
    env = config["env"]
    host = config["host"]
    orgs = config["orgs"]
    org_dict = {org["name"]: org["cred"] for org in orgs}
    SETTINGS[env] = {"host": host, "orgs": org_dict}

st.sidebar.markdown("#### **Settings**")
select_env = st.sidebar.selectbox("Environment", list(SETTINGS.keys()), on_change=reset_session_state)
select_org = st.sidebar.selectbox(
    "Organization",
    list(SETTINGS[select_env]["orgs"].keys()),
    on_change=reset_session_state,
)

# Hack the sidebar size to be a little bit smaller
st.markdown(
    """
        <style>
            .sidebar .sidebar-content {
                width: 375px;
            }
        </style>
    """,
    unsafe_allow_html=True,
)

# Initialize session state
if "client" not in st.session_state:
    st.session_state.client = SkyvernClient(
        base_url=SETTINGS[select_env]["host"],
        credentials=SETTINGS[select_env]["orgs"][select_org],
    )
if "repository" not in st.session_state:
    st.session_state.repository = TaskRepository(st.session_state.client)
if "task_page_number" not in st.session_state:
    st.session_state.task_page_number = 1
if "selected_task" not in st.session_state:
    st.session_state.selected_task = None
    st.session_state.selected_task_recording_uri = None
    st.session_state.task_steps = None
if "selected_step" not in st.session_state:
    st.session_state.selected_step = None
    st.session_state.selected_step_index = None

client = st.session_state.client
repository = st.session_state.repository
task_page_number = st.session_state.task_page_number
selected_task = st.session_state.selected_task
selected_task_recording_uri = st.session_state.selected_task_recording_uri
task_steps = st.session_state.task_steps
selected_step = st.session_state.selected_step
selected_step_index = st.session_state.selected_step_index


# Onclick handlers
def select_task(task: dict) -> None:
    st.session_state.selected_task = task
    st.session_state.selected_task_recording_uri = repository.get_task_recording_uri(task)
    # reset step selection
    st.session_state.selected_step = None
    # save task's steps in session state
    st.session_state.task_steps = repository.get_task_steps(task["task_id"])
    if st.session_state.task_steps:
        st.session_state.selected_step = st.session_state.task_steps[0]
        st.session_state.selected_step_index = 0


def go_to_previous_step() -> None:
    new_step_index = max(0, selected_step_index - 1)
    select_step(task_steps[new_step_index])


def go_to_next_step() -> None:
    new_step_index = min(len(task_steps) - 1, selected_step_index + 1)
    select_step(task_steps[new_step_index])


def select_step(step: dict) -> None:
    st.session_state.selected_step = step
    st.session_state.selected_step_index = task_steps.index(step)


# Streamlit UI Logic
st.markdown("# **:dragon: Skyvern :dragon:**")
st.markdown(f"### **{select_env} - {select_org}**")
execute_tab, visualizer_tab = st.tabs(["Execute", "Visualizer"])


def copy_curl_to_clipboard(task_request_body: TaskRequest) -> None:
    clipboard.copy(client.copy_curl(task_request_body=task_request_body))


with execute_tab:
    # Streamlit doesn't support "focusing" on a tab, so this is a workaround to make the requested tab be the "first" tab
    sorted_supported_examples = sorted(
        supported_examples,
        key=lambda x: (-1 if x.name.lower() == tab_name.lower() else 0),
    )
    example_tabs = st.tabs([supported_example.name for supported_example in sorted_supported_examples])

    for i, example_tab in enumerate(example_tabs):
        with example_tab:
            create_column, explanation_column = st.columns([2, 3])
            with create_column:
                run_task, copy_curl = st.columns([3, 1])
                task_request_body = sorted_supported_examples[i]

                unique_key = f"{task_request_body.name}"
                copy_curl.button(
                    "Copy cURL",
                    key=f"copy_curl_{unique_key}",
                    on_click=lambda: copy_curl_to_clipboard(task_request_body=task_request_body),
                )
                with st.form(f"task_form_{unique_key}"):
                    run_task.markdown("## Run a task")

                    example = sorted_supported_examples[i]
                    # Create all the fields to create a TaskRequest object
                    st_url = st.text_input("URL*", value=example.url, key=f"url_{unique_key}")
                    st_webhook_callback_url = st.text_input(
                        "Webhook Callback URL",
                        key=f"webhook_{unique_key}",
                        placeholder="Optional",
                    )
                    st_navigation_goal = st.text_area(
                        "Navigation Goal",
                        key=f"nav_goal_{unique_key}",
                        placeholder="Describe the navigation goal",
                        value=example.navigation_goal,
                        height=120,
                    )
                    st_data_extraction_goal = st.text_area(
                        "Data Extraction Goal",
                        key=f"data_goal_{unique_key}",
                        placeholder="Describe the data extraction goal",
                        value=example.data_extraction_goal,
                        height=120,
                    )
                    st_navigation_payload = st.text_area(
                        "Navigation Payload JSON",
                        key=f"nav_payload_{unique_key}",
                        placeholder='{"name": "John Doe", "email": "abc@123.com"}',
                        value=json.dumps(example.navigation_payload, indent=2),
                        height=200,
                    )
                    st_extracted_information_schema = st.text_area(
                        "Extracted Information Schema",
                        key=f"extracted_info_schema_{unique_key}",
                        placeholder='{"quote_price": "float"}',
                        value=example.extracted_information_schema,
                    )
                    # Create a TaskRequest object from the form fields
                    task_request_body = TaskRequest(
                        url=st_url,
                        webhook_callback_url=st_webhook_callback_url,
                        navigation_goal=st_navigation_goal,
                        data_extraction_goal=st_data_extraction_goal,
                        proxy_location=ProxyLocation.NONE,
                        navigation_payload=st_navigation_payload,
                        extracted_information_schema=st_extracted_information_schema,
                    )
                    # Submit the form
                    if st.form_submit_button("Execute Task", use_container_width=True):
                        # Call the API to create a task
                        task_id = client.create_task(task_request_body)
                        if not task_id:
                            st.error("Failed to create task!")
                        else:
                            st.success("Task created successfully, task_id: " + task_id)

            with explanation_column:
                st.markdown("### **Task Request**")
                st.markdown("\n")
                st.markdown("#### **URL**")
                st.markdown("The starting URL for the task.")
                st.markdown("\n")
                st.markdown("#### **Webhook Callback URL**")
                st.markdown("The URL to call with the results when the task is completed.")
                st.markdown("\n")
                st.markdown("#### **Navigation Goal**")
                st.markdown("The user's goal for the task. Nullable if the task is only for data extraction.")
                st.markdown("\n")
                st.markdown("\n")
                st.markdown("#### **Data Extraction Goal**")
                st.markdown("The user's goal for data extraction. Nullable if the task is only for navigation.")
                st.markdown("\n")
                st.markdown("\n")
                st.markdown("#### **Navigation Payload**")
                st.markdown(
                    "The user's details needed to achieve the task. This is an unstructured field, and information can be passed in in any format you desire. Skyvern will map this information to the questions on the screen in real-time"
                )
                st.markdown("\n")
                st.markdown("\n")
                st.markdown("\n")
                st.markdown("#### **Extracted Information Schema**")
                st.markdown(
                    "(Optional) The requested schema of the extracted information for data extraction goal. This is a JSON object with keys as the field names and values as the data types. The data types can be any of the following: string, number, boolean, date, datetime, time, float, integer, object, array, null. If the schema is not provided, Skyvern will infer the schema from the extracted data."
                )


with visualizer_tab:
    task_id_input = st.text_input("task_id", value="")

    def search_task() -> None:
        if not task_id_input:
            return

        task = repository.get_task(task_id_input)
        if task:
            select_task(task)
        else:
            st.error(f"Task with id {task_id_input} not found.")

    st.button("search task", on_click=search_task)

    col_tasks, _, col_steps, _, col_artifacts = st.columns([4, 1, 6, 1, 18])

    col_tasks.markdown("#### Tasks")
    col_steps.markdown("#### Steps")
    col_artifacts.markdown("#### Artifacts")
    tasks_response = repository.get_tasks(task_page_number)
    if not isinstance(tasks_response, list):
        st.error("Failed to fetch tasks.")
        st.error(tasks_response)
        st.error(
            "#1 -- Make sure you have both the server (./run_skyvern.sh) and client (./run_ui.sh) running at the same time in different terminals."
        )
        st.error(
            "#2 -- If you're getting a credentials error, Make sure you have the correct organization credentials in .streamlit/secrets.toml."
        )
        st.error(
            "You can validate the credentials against the postgresql credentials by running\n\n"
            '`psql -U skyvern -h localhost -d skyvern -c "SELECT o.organization_id, o.organization_name, token FROM organizations o JOIN organization_auth_tokens oat ON oat.organization_id = o.organization_id;"`.'
            "\n\n NOTE: There might be multiple organizations -- each run of ./setup.sh creates a new one. Pick your favourite!"
            "\n\n If you're running postgres via Docker, please make sure you wrap it in a docker exec command. "
            "`docker exec postgresql-container psql -U skyvern -h localhost -d skyvern -c 'SELECT o.organization_id, o.organization_name, token FROM organizations o JOIN organization_auth_tokens oat ON oat.organization_id = o.organization_id;'`"
        )

    else:
        # Display tasks in sidebar for selection
        tasks = {task["task_id"]: task for task in tasks_response}
        task_id_buttons = {
            task_id: col_tasks.button(
                f"{task_id}",
                on_click=select_task,
                args=(task,),
                use_container_width=True,
                type=("primary" if selected_task and task_id == selected_task["task_id"] else "secondary"),
            )
            for task_id, task in tasks.items()
        }

        # Display pagination buttons
        task_page_prev, _, show_task_page_number, _, task_page_next = col_tasks.columns([1, 1, 1, 1, 1])
        show_task_page_number.button(str(task_page_number), disabled=True)
        if task_page_next.button("\>"):
            st.session_state.task_page_number += 1
        if task_page_prev.button("\<", disabled=task_page_number == 1):
            st.session_state.task_page_number = max(1, st.session_state.task_page_number - 1)

        (
            tab_task,
            tab_step,
            tab_recording,
            tab_screenshot,
            tab_post_action_screenshot,
            tab_id_to_xpath,
            tab_id_to_frame,
            tab_element_tree,
            tab_element_tree_trimmed,
            tab_llm_prompt,
            tab_llm_request,
            tab_llm_response_parsed,
            tab_llm_response_raw,
            tab_html,
        ) = col_artifacts.tabs(
            [
                ":green[Task]",
                ":blue[Step]",
                ":violet[Recording]",
                ":rainbow[Screenshot]",
                ":rainbow[Action Screenshots]",
                ":red[ID -> XPath]",
                ":red[ID -> Frame]",
                ":orange[Element Tree]",
                ":blue[Element Tree (Trimmed)]",
                ":yellow[LLM Prompt]",
                ":green[LLM Request]",
                ":blue[LLM Response (Parsed)]",
                ":violet[LLM Response (Raw)]",
                ":rainbow[Html (Raw)]",
            ]
        )

        tab_task_details, tab_task_steps, tab_task_action_results = tab_task.tabs(
            ["Details", "Steps", "Action Results"]
        )

        if selected_task:
            tab_task_details.json(selected_task)
            if selected_task_recording_uri:
                streamlit_show_recording(tab_recording, selected_task_recording_uri)

            if task_steps:
                col_steps_prev, _, col_steps_next = col_steps.columns([3, 1, 3])
                col_steps_prev.button(
                    "prev",
                    on_click=go_to_previous_step,
                    key="previous_step_button",
                    use_container_width=True,
                )
                col_steps_next.button(
                    "next",
                    on_click=go_to_next_step,
                    key="next_step_button",
                    use_container_width=True,
                )

            step_id_buttons = {
                step["step_id"]: col_steps.button(
                    f"{step['order']} - {step['retry_index']} - {step['step_id']}",
                    on_click=select_step,
                    args=(step,),
                    use_container_width=True,
                    type=("primary" if selected_step and step["step_id"] == selected_step["step_id"] else "secondary"),
                )
                for step in task_steps
            }

            df = pd.json_normalize(task_steps)
            tab_task_steps.dataframe(df, use_container_width=True, height=1000)

            task_action_results = []
            for step in task_steps:
                output = step.get("output")
                step_id = step["step_id"]
                if output:
                    step_action_results = output.get("action_results", [])
                    for action_result in step_action_results:
                        task_action_results.append(
                            {
                                "step_id": step_id,
                                "order": step["order"],
                                "retry_index": step["retry_index"],
                                **action_result,
                            }
                        )
            df = pd.json_normalize(task_action_results)
            df = df.reindex(sorted(df.columns), axis=1)
            tab_task_action_results.dataframe(df, use_container_width=True, height=1000)

            if selected_step:
                tab_step.json(selected_step)

                artifacts_response = repository.get_artifacts(selected_task["task_id"], selected_step["step_id"])
                split_artifact_uris = [artifact["uri"].split("/") for artifact in artifacts_response]
                file_name_to_uris = {split_uri[-1]: "/".join(split_uri) for split_uri in split_artifact_uris}

                for file_name, uri in file_name_to_uris.items():
                    file_name = file_name.lower()
                    if file_name.endswith("screenshot_llm.png") or file_name.endswith("screenshot.png"):
                        streamlit_content_safe(
                            tab_screenshot,
                            tab_screenshot.image,
                            read_artifact_safe(uri, is_image=True),
                            "No screenshot available.",
                            use_column_width=True,
                        )
                    elif file_name.endswith("screenshot_action.png"):
                        streamlit_content_safe(
                            tab_post_action_screenshot,
                            tab_post_action_screenshot.image,
                            read_artifact_safe(uri, is_image=True),
                            "No action screenshot available.",
                            use_column_width=True,
                        )
                    elif file_name.endswith("id_xpath_map.json"):
                        streamlit_content_safe(
                            tab_id_to_xpath,
                            tab_id_to_xpath.json,
                            read_artifact_safe(uri),
                            "No ID -> XPath map available.",
                        )
                    elif file_name.endswith("id_frame_map.json"):
                        streamlit_content_safe(
                            tab_id_to_frame,
                            tab_id_to_frame.json,
                            read_artifact_safe(uri),
                            "No ID -> Frame map available.",
                        )
                    elif file_name.endswith("tree.json"):
                        streamlit_content_safe(
                            tab_element_tree,
                            tab_element_tree.json,
                            read_artifact_safe(uri),
                            "No element tree available.",
                        )
                    elif file_name.endswith("tree_trimmed.json"):
                        streamlit_content_safe(
                            tab_element_tree_trimmed,
                            tab_element_tree_trimmed.json,
                            read_artifact_safe(uri),
                            "No element tree trimmed available.",
                        )
                    elif file_name.endswith("llm_prompt.txt"):
                        content = read_artifact_safe(uri)
                        # this is a hacky way to call this generic method to get it working with st.text_area
                        streamlit_content_safe(
                            tab_llm_prompt,
                            tab_llm_prompt.text_area,
                            content,
                            "No LLM prompt available.",
                            value=content,
                            height=1000,
                            label_visibility="collapsed",
                        )
                        # tab_llm_prompt.text_area("collapsed", value=content, label_visibility="collapsed", height=1000)
                    elif file_name.endswith("llm_request.json"):
                        streamlit_content_safe(
                            tab_llm_request,
                            tab_llm_request.json,
                            read_artifact_safe(uri),
                            "No LLM request available.",
                        )
                    elif file_name.endswith("llm_response_parsed.json"):
                        streamlit_content_safe(
                            tab_llm_response_parsed,
                            tab_llm_response_parsed.json,
                            read_artifact_safe(uri),
                            "No parsed LLM response available.",
                        )
                    elif file_name.endswith("llm_response.json"):
                        streamlit_content_safe(
                            tab_llm_response_raw,
                            tab_llm_response_raw.json,
                            read_artifact_safe(uri),
                            "No raw LLM response available.",
                        )
                    elif file_name.endswith("html_scrape.html"):
                        streamlit_content_safe(
                            tab_html,
                            tab_html.text,
                            read_artifact_safe(uri),
                            "No html available.",
                        )
                    elif file_name.endswith("html_action.html"):
                        streamlit_content_safe(
                            tab_html,
                            tab_html.text,
                            read_artifact_safe(uri),
                            "No html available.",
                        )
                    else:
                        st.write(f"Artifact {file_name} not supported.")
