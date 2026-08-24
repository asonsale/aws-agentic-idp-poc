import os
import time

import streamlit as st
import requests

GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://gateway:8000")

st.set_page_config(page_title="Enterprise Agentic RAG Chatbot", page_icon="🤖", layout="wide")

st.title("🤖 AWS Enterprise Agentic RAG & IDP Chatbot (POC)")
st.caption("Powered by Amazon Bedrock, LangGraph, Model Context Protocol (MCP), and Microsoft Presidio")

with st.sidebar:
    st.subheader("Session context")
    tenant = st.selectbox("Tenant department", ["risk_dept_01", "finance_dept_01"])
    st.caption("Switch tenants to verify data isolation -- each tenant should "
               "only see its own account data and document context.")

if "messages" not in st.session_state:
    st.session_state.messages = []

for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

if prompt := st.chat_input("Ask about company policies, account risk status, or server diagnostics..."):
    st.chat_message("user").markdown(prompt)
    st.session_state.messages.append({"role": "user", "content": prompt})

    with st.chat_message("assistant"):
        with st.status("Executing Agentic Pipeline...", expanded=True) as status:
            st.write("🔍 **Tier 1:** Running Presidio PII detection + in-app masking...")
            start_time = time.time()

            try:
                resp = requests.post(
                    f"{GATEWAY_URL}/api/v1/query",
                    json={"user_id": "emp_4402", "tenant_department": tenant, "prompt": prompt},
                    timeout=60,
                ).json()
            except Exception as e:
                resp = {"response": f"Gateway unreachable: {e}"}

            elapsed = round(time.time() - start_time, 2)
            st.write("📚 **Tier 2:** Querying pgvector for tenant-scoped context...")
            st.write("🔌 **Tier 2 (MCP):** Executing FastMCP tool call over SSE to RDS...")
            st.write("⚡ **Tier 3:** Invoking Claude on Amazon Bedrock Runtime...")

            status.update(label=f"Execution completed in {elapsed}s!", state="complete", expanded=False)

        final_output = resp.get("response", "Error executing request.")
        st.markdown(final_output)
        st.session_state.messages.append({"role": "assistant", "content": final_output})
