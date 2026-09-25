"""Generate the messy multi-source fixtures used by tests and the demo.

All agencies here are FICTIONAL ("Town of Pinecrest", "Harlow County").
The files reproduce failure modes seen in real agency exports; the data
itself is made up and must never be charted as real crime.

    python scripts/make_fixtures.py
"""
from __future__ import annotations

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

FIX = Path(__file__).resolve().parents[1] / "tests" / "fixtures"


def csv_fixtures() -> None:
    # 1. Preamble above header, cp1252 bytes (curly apostrophe, en dash), US dates, footer total row.
    pinecrest_aug = (
        "Town of Pinecrest Police Department\r\n"
        "Incident Export – Report generated 09/03/2026 08:14 AM\r\n"
        "\r\n"
        "Case #,Reported Date,Offense Date,NIBRS Code,Offense Description,Street Address,Town,Status\r\n"
        "P26-004411,08/01/2026,07/31/2026,23F,Theft From Motor Vehicle,100 MAIN ST,PINECREST,Open\r\n"
        "P26-004412,08/02/2026,08/02/2026,13A,Aggravated Assault,\"45 OAK AVE, APT 3\",PINECREST,Cleared by Arrest\r\n"
        "P26-004419,08/05/2026,08/04/2026,220,Burglary/B&E,700 ELM ST,PINECREST,Open\r\n"
        "P26-004420,08/07/2026,08/09/2026,23C,Shoplifting,1200 COMMERCE BLVD,PINECREST,Open\r\n"
        "P26-004425,08/12/2026,,290,Damage/Vandalism Of Property,8 CHURCH ST,PINECREST,Exceptionally Cleared\r\n"
        "P26-004431,08/19/2026,08/19/2026,120,Robbery – owner’s store,1200 COMMERCE BLVD,PINECREST,Open\r\n"
        "P26-004433,8/22/26,8/21/26,240,Motor Vehicle Theft,55 LAKE DR,PINECRST,Open\r\n"
        "P26-004440,08/30/2026,08/29/2026,23H,All Other Thefts,300 MILL RD,PINECREST,Unfounded\r\n"
        "Total:,8,,,,,,\r\n"
    )
    (FIX / "pinecrest_pd_2026-08.csv").write_bytes(pinecrest_aug.encode("cp1252"))

    # 2. Next month, same agency: headers renamed and reordered, a new column,
    #    and a different date format. Positional parsing would silently corrupt this.
    pinecrest_sep = (
        "Report Number,Offense,Code,Date of Report,Date of Offense,Location,Municipality,Case Status,Officer Unit\n"
        "P26-004501,Shoplifting,23C,\"Sep 02, 2026\",\"Sep 01, 2026\",1200 COMMERCE BLVD,PINECREST,Open,B2\n"
        "P26-004507,Simple Assault,13B,\"Sep 06, 2026\",\"Sep 06, 2026\",9 PARK PL,PINECREST,Cleared by Arrest,A1\n"
        "P26-004512,Burglary/B&E,220,\"Sep 11, 2026\",\"Sep 10, 2026\",700 ELM ST,PINECREST,Open,B2\n"
        "P26-004518,Theft From Motor Vehicle,23F,\"Sep 15, 2026\",\"Sep 14, 2026\",100 MAIN ST,PINECREST,Open,C3\n"
    )
    (FIX / "pinecrest_pd_2026-09.csv").write_text(pinecrest_sep, encoding="utf-8")

    # 3. Semicolon-delimited county export, ISO dates, with planted defects:
    #    duplicate key, future date, blank offense code, coordinates outside the
    #    county, occurred-after-reported, and an unmapped new column.
    harlow = (
        "REPORT_NUMBER;REPORT_DATE;INCIDENT_DATE;UCR_CODE;CRIME_TYPE;LOCATION;JURISDICTION;DISPOSITION;LATITUDE;LONGITUDE;OFFICER_BADGE\n"
        "HC-2026-0801;2026-08-03;2026-08-02;240;Motor Vehicle Theft;RT 9 & OLD FARM RD;HARLOW CO;Open;35.21;-80.84;1142\n"
        "HC-2026-0802;2026-08-04;2026-08-04;13B;Simple Assault;12 RIVER RD;HARLOW CO;Cleared by Arrest;35.30;-80.71;1088\n"
        "HC-2026-0802;2026-08-04;2026-08-04;13B;Simple Assault;12 RIVER RD;HARLOW CO;Cleared by Arrest;35.30;-80.71;1088\n"
        "HC-2026-0803;2026-08-09;2026-08-11;220;Burglary/B&E;400 QUARRY LN;HARLOW CO;Open;35.25;-80.90;1142\n"
        "HC-2026-0804;2027-08-10;2026-08-10;23H;All Other Thefts;77 FERRY ST;HARLOW CO;Open;35.26;-80.88;1201\n"
        "HC-2026-0805;2026-08-14;2026-08-13;;Suspicious Activity;5 TOWER RD;HARLOW CO;Open;35.24;-80.86;1201\n"
        "HC-2026-0806;2026-08-20;2026-08-20;23F;Theft From Motor Vehicle;90 PINE ST;HARLOW CO;Open;3.524;-80.86;1088\n"
    )
    (FIX / "harlow_so_2026-08.csv").write_text(harlow, encoding="utf-8")


def _pdf(path: Path, agency: str, period: str, header: list[str], rows: list[list[str]],
         repeat_on: int | None = None) -> None:
    styles = getSampleStyleSheet()
    story = [Paragraph(f"<b>Monthly Offense Summary</b>", styles["Title"]),
             Paragraph(f"Agency: {agency}", styles["Normal"]),
             Paragraph(f"Reporting Period: {period}", styles["Normal"]),
             Paragraph("Counts reflect highest offense per report. * includes reclassified reports.", styles["Italic"]),
             Spacer(1, 12)]
    style = TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                        ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold")])
    chunks = [rows] if repeat_on is None else [rows[:repeat_on], rows[repeat_on:]]
    for i, chunk in enumerate(chunks):
        t = Table([header, *chunk], style=style)
        story.append(t)
        if i < len(chunks) - 1:
            from reportlab.platypus import PageBreak
            story.append(PageBreak())
    SimpleDocTemplate(str(path), pagesize=letter).build(story)


def pdf_fixtures() -> None:
    # 2025 layout: Offense | NIBRS Code | Month Count | Prior Year Same Month
    _pdf(FIX / "pinecrest_summary_2025-08.pdf", "Town of Pinecrest Police Department", "August 2025",
         ["Offense", "NIBRS Code", "Month Count", "Prior Year Same Month"],
         [["Aggravated Assault", "13A", "3", "2"], ["Robbery", "120", "1", "—"],
          ["Burglary/B&E", "220", "6", "9"], ["Shoplifting", "23C", "14", "11"],
          ["Theft From Motor Vehicle", "23F", "22", "19"], ["Motor Vehicle Theft", "240", "4", "5"],
          ["Total", "", "50", "46"]])
    # 2026 layout: columns reordered and renamed, a % Change column added,
    # table split across two pages with the header repeated, footnote markers,
    # thousands separators, and one unparseable cell that must be flagged.
    _pdf(FIX / "pinecrest_summary_2026-08.pdf", "Town of Pinecrest Police Department", "Aug. 2026",
         ["Code", "Offense Category", "Same Month Last Year", "Current Month", "% Change"],
         [["13A", "Aggravated Assault", "3", "5*", "+66.7%"], ["120", "Robbery", "1", "—", "-100%"],
          ["220", "Burglary/B&E", "6", "7", "+16.7%"], ["23C", "Shoplifting", "14", "1,012", "n/m"],
          ["23F", "Theft From Motor Vehicle", "22", "18", "-18.2%"],
          ["240", "Motor Vehicle Theft", "4", "6", "+50.0%"], ["290", "Damage/Vandalism", "9", "n/a", ""],
          ["Total", "", "59", "1,048", ""]],
         repeat_on=4)


if __name__ == "__main__":
    FIX.mkdir(parents=True, exist_ok=True)
    csv_fixtures()
    pdf_fixtures()
    print("fixtures written to", FIX)
