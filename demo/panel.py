"""Streamlit demo panel: chat, SQL, feedback, upload, onboarding wizard.

Run: uv run streamlit run demo/panel.py
Configure: QUERY_URL (default http://localhost:8000) and TENANT (default demo).
"""

import json
import os

import requests
import streamlit as st

QUERY_URL = os.environ.get("QUERY_URL", "http://localhost:8000")
TENANT = os.environ.get("TENANT", "demo")

st.set_page_config(page_title="QueryPulse", page_icon="📊", layout="centered")
st.title("QueryPulse")
st.caption(f"NL → SQL over telemetry · tenant `{TENANT}` · API `{QUERY_URL}`")

tab_chat, tab_docs, tab_wizard = st.tabs(["Chat", "Documents", "Onboarding"])


def envelope_ok(payload: dict[str, object]) -> bool:
    """Whether an API envelope reports success."""
    return payload.get("status") == "Success"


with tab_chat:
    if "session_id" not in st.session_state:
        st.session_state.session_id = None
    if "messages" not in st.session_state:
        st.session_state.messages = []
    if "last_history_id" not in st.session_state:
        st.session_state.last_history_id = None

    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])
            if message.get("sql"):
                with st.expander("Generated SQL"):
                    st.code(message["sql"], language="sql")
                if message.get("assumption"):
                    st.info(message["assumption"])

    question = st.chat_input("Ask about your telemetry…")
    if question:
        st.session_state.messages.append({"role": "user", "content": question})
        with st.chat_message("user"):
            st.markdown(question)

        body = {"tenant": TENANT, "query": question}
        if st.session_state.session_id:
            body["sessionId"] = st.session_state.session_id
        try:
            response = requests.post(f"{QUERY_URL}/v1/query/sql", json=body, timeout=60)
            payload = response.json()
        except requests.RequestException as exc:
            payload = {"status": "Failure", "message": f"API unreachable: {exc}"}

        with st.chat_message("assistant"):
            if envelope_ok(payload) and payload.get("data"):
                data = payload["data"]
                st.markdown(data.get("summary", ""))
                if data.get("rows"):
                    st.dataframe(data["rows"])
                with st.expander("Generated SQL"):
                    st.code(data.get("sql", ""), language="sql")
                if data.get("assumptionNote"):
                    st.info(data["assumptionNote"])
                st.session_state.session_id = data.get("sessionId")
                st.session_state.last_history_id = data.get("historyId")
                reply = {
                    "role": "assistant",
                    "content": data.get("summary", ""),
                    "sql": data.get("sql"),
                    "assumption": data.get("assumptionNote"),
                }
            elif payload.get("reply"):
                st.markdown(payload["reply"])
                reply = {"role": "assistant", "content": payload["reply"]}
            else:
                st.error(payload.get("message", "request failed"))
                reply = {"role": "assistant", "content": payload.get("message", "error")}
        st.session_state.messages.append(reply)

    if st.session_state.last_history_id:
        cols = st.columns(2)
        if cols[0].button("👍 Good answer", use_container_width=True):
            requests.post(
                f"{QUERY_URL}/v1/feedback",
                json={"historyId": st.session_state.last_history_id, "rating": "up"},
                timeout=10,
            )
            st.toast("Thanks — recorded")
        if cols[1].button("👎 Wrong answer", use_container_width=True):
            requests.post(
                f"{QUERY_URL}/v1/feedback",
                json={"historyId": st.session_state.last_history_id, "rating": "down"},
                timeout=10,
            )
            st.toast("Recorded — a reviewer will see it")

with tab_docs:
    st.subheader("Upload maintenance documents")
    uploaded = st.file_uploader(
        "Markdown, HTML, PDF, DOCX, or XLSX (≤20 MB)",
        type=["md", "txt", "html", "htm", "pdf", "docx", "xlsx"],
    )
    if uploaded is not None and st.button("Ingest", type="primary"):
        files = {"file": (uploaded.name, uploaded.getvalue(), uploaded.type)}
        response = requests.post(
            f"{QUERY_URL}/v1/documents", data={"tenant": TENANT}, files=files, timeout=120
        )
        payload = response.json()
        if envelope_ok(payload):
            data = payload["data"]
            st.success(f"Ingested {data['filename']}: {data['chunkCount']} chunks")
        else:
            st.error(payload.get("message", "ingest failed"))

    if st.button("List documents"):
        response = requests.get(f"{QUERY_URL}/v1/documents", params={"tenant": TENANT}, timeout=10)
        payload = response.json()
        if envelope_ok(payload) and payload["data"]:
            st.table(
                [
                    {
                        "file": doc["filename"],
                        "chunks": doc["chunkCount"],
                        "pages": doc["totalPages"],
                    }
                    for doc in payload["data"]
                ]
            )
        else:
            st.info("No documents ingested yet")

with tab_wizard:
    st.subheader(f"Onboarding · {TENANT}")
    if st.button("Probe telemetry keys"):
        response = requests.get(f"{QUERY_URL}/v1/onboarding/{TENANT}/probe", timeout=60)
        payload = response.json()
        if envelope_ok(payload):
            data = payload["data"]
            st.table([{"key": k["key"], "samples": k["sampleCount"]} for k in data["keys"]])
        else:
            st.error(payload.get("message", "probe failed"))

    if st.button("Readiness check"):
        response = requests.get(f"{QUERY_URL}/v1/onboarding/{TENANT}/readiness", timeout=10)
        st.json(json.dumps(response.json().get("data", {})))

    cols = st.columns(2)
    if cols[0].button("Enable tenant"):
        response = requests.post(f"{QUERY_URL}/v1/onboarding/{TENANT}/enable", timeout=10)
        st.write(response.json().get("message"))
    if cols[1].button("Disable tenant"):
        response = requests.post(f"{QUERY_URL}/v1/onboarding/{TENANT}/disable", timeout=10)
        st.write(response.json().get("message"))
