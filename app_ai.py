import io
import concurrent.futures as cf

import pandas as pd
import streamlit as st

from logic_ai import (
    ApiRotationManager,
    classify_row,
    DEFAULT_THIRD_PARTY_DOMAINS,
    DEFAULT_TRACKING_PARAMS,
)

MAX_WORKERS_DEFAULT = 12


def run_ai_validator():
    st.title("Campaign URL Validator")
    st.caption(
        "Upload the AMP/dealer profile export, pick your URL columns, and run automated "
        "checks for the issue types in the RAPP QA matrix."
    )

    with st.sidebar:
        st.header("1. Upload file")
        uploaded = st.file_uploader("Excel file (.xlsx)", type=["xlsx"])

        st.header("2. Settings")
        tracking_input = st.text_area(
            "Tracking query-string params to flag (one per line)",
            value="\n".join(DEFAULT_TRACKING_PARAMS),
            height=100,
        )
        third_party_domains_input = st.text_area(
            "3rd party scheduler domains (one per line)",
            value="\n".join(DEFAULT_THIRD_PARTY_DOMAINS),
            height=100,
        )
        validation_types = st.multiselect(
            "Validation types to run",
            options=["Query String Tests", "Tracking Detection", "Domain Verification"],
            default=["Query String Tests", "Tracking Detection", "Domain Verification"],
        )
        run_bonus_validation = st.checkbox(
            "Bonus: run replacement-URL suggestion using sitemap and homepage links (slower)",
            value=False,
        )
        run_ai_validation = st.checkbox(
            "AI-based bonus validation via Gemini (requires API key)",
            value=False,
        )
        ai_api_keys_input = st.text_area(
            "Gemini API keys (one per line)",
            value="",
            height=100,
        )
        gemini_model = st.text_input(
            "Gemini model to use",
            value="gemini-3-flash-preview",
        )
        show_ai_payload = st.checkbox(
            "Show AI request payload for debugging",
            value=False,
        )
        max_workers = st.slider("Parallel requests", min_value=1, max_value=24, value=MAX_WORKERS_DEFAULT)

    if uploaded is None:
        st.info("Upload an .xlsx file in the sidebar to get started.")
        st.stop()

    df = pd.read_excel(uploaded)
    st.subheader("Preview")
    st.dataframe(df.head(20), use_container_width=True)

    cols = list(df.columns)

    def guess_col(possible_names):
        for name in possible_names:
            for c in cols:
                if c.strip().lower() == name.lower():
                    return c
        return cols[0]

    col1, col2 = st.columns(2)
    with col1:
        dealer_url_col = st.selectbox(
            "Dealer_URL column (homepage)",
            cols,
            index=cols.index(guess_col(["DEALER_URL", "Dealer_URL"])),
        )
    with col2:
        service_url_col = st.selectbox(
            "Dealer_Service_URL column to validate",
            cols,
            index=cols.index(guess_col(["DEALER_SERVICE_URL", "Dealer_Service_URL"])),
        )

    row_limit = st.number_input(
        "Rows to process (0 = all rows — full file can take a while)",
        min_value=0,
        value=min(50, len(df)),
        step=10,
    )

    run = st.button("Run validation", type="primary")

    if run:
        tracking_params = [p.strip() for p in tracking_input.splitlines() if p.strip()]
        third_party_domains = [p.strip() for p in third_party_domains_input.splitlines() if p.strip()]
        run_qs_test = "Query String Tests" in validation_types
        run_tracking_detection = "Tracking Detection" in validation_types
        run_domain_checks = "Domain Verification" in validation_types
        run_ai = run_ai_validation and bool(ai_api_keys_input.strip())
        ai_api_keys = [p.strip() for p in ai_api_keys_input.splitlines() if p.strip()]
        rotation_manager = ApiRotationManager(ai_api_keys) if run_ai else None
        run_ai_debug = show_ai_payload

        work_df = df.copy()
        if row_limit and row_limit > 0:
            work_df = work_df.iloc[:row_limit].copy()

        progress = st.progress(0, text="Starting...")
        results = [None] * len(work_df)

        def process_index(i, row):
            dealer_url = str(row.get(dealer_url_col, "") or "")
            service_url = str(row.get(service_url_col, "") or "")
            return i, classify_row(
                dealer_url,
                service_url,
                third_party_domains=third_party_domains,
                tracking_params=tracking_params,
                run_query_string_test=run_qs_test,
                run_domain_checks=run_domain_checks,
                run_tracking_detection=run_tracking_detection,
                run_sitemap_suggestion=run_bonus_validation,
                run_homepage_suggestion=run_bonus_validation,
                run_ai_validation=run_ai,
                ai_api_keys=ai_api_keys,
                ai_rotation_manager=rotation_manager,
                gemini_model=gemini_model,
            )

        with cf.ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = [ex.submit(process_index, i, row) for i, row in work_df.iterrows()]
            done = 0
            total = len(futures)
            for fut in cf.as_completed(futures):
                i, res = fut.result()
                results[work_df.index.get_loc(i)] = res
                done += 1
                progress.progress(done / total, text=f"Processed {done}/{total}")

        progress.empty()

        out_df = work_df.copy()
        out_df["Error_Type"] = [r["Error_Type"] for r in results]
        out_df["Notes"] = [r["Notes"] for r in results]
        out_df["Suggested_New_URL"] = [r["Suggested_New_URL"] for r in results]
        out_df["Suggested_URL_Confidence"] = [r["Suggested_URL_Confidence"] for r in results]
        out_df["AI_Suggested_URL"] = [r.get("AI_Suggested_URL", "") for r in results]
        out_df["AI_Suggested_URL_Confidence"] = [r.get("AI_Suggested_URL_Confidence", "") for r in results]
        out_df["AI_Suggested_URL_Validation"] = [r.get("AI_Suggested_URL_Validation", "") for r in results]
        out_df["AI_Suggested_URL_Validation_Notes"] = [r.get("AI_Suggested_URL_Validation_Notes", "") for r in results]
        out_df["AI_Debug_Message"] = [r.get("AI_Debug_Message", "") for r in results]

        st.subheader("Results")
        st.dataframe(out_df, use_container_width=True)

        if rotation_manager is not None:
            st.subheader("API rotation status")
            api_rotation_df = pd.DataFrame(rotation_manager.get_status_records())
            st.dataframe(api_rotation_df, use_container_width=True)

        if show_ai_payload:
            debug_messages = [msg for msg in out_df["AI_Debug_Message"].unique() if msg]
            if debug_messages:
                st.subheader("AI debug payloads")
                for idx, msg in enumerate(debug_messages, start=1):
                    with st.expander(f"AI payload {idx}"):
                        st.code(msg)

        st.subheader("Summary")
        st.markdown(
            f"**Validation categories enabled:** {', '.join(validation_types) if validation_types else 'None (basic reachability checks only)'}"
        )
        st.dataframe(out_df["Error_Type"].value_counts().rename_axis("Error_Type").reset_index(name="Count"))

        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            out_df.to_excel(writer, index=False, sheet_name="Validated")
            if rotation_manager is not None:
                pd.DataFrame(rotation_manager.get_status_records()).to_excel(writer, index=False, sheet_name="API_Rotation")
        buf.seek(0)

        st.download_button(
            "Download results as .xlsx",
            data=buf,
            file_name="dealer_service_url_validation_results.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )


def main():
    st.set_page_config(page_title="Campaign URL Validator", layout="wide")
    run_ai_validator()


if __name__ == "__main__":
    main()
