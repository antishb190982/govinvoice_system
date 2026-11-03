"""Render the source-controlled deployment guide to a downloadable PDF."""
import argparse
import re
from html import escape
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    CondPageBreak, HRFlowable, LongTable, PageBreak, Paragraph, Preformatted, SimpleDocTemplate, Spacer, TableStyle,
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    server = root / "server" if (root / "server" / "manage.py").exists() else root
    source = server / "docs" / "on-premises-deployment.md"
    lines = source.read_text().splitlines()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(
        name="BodyGuide", fontName="Helvetica", fontSize=9.5, leading=13.5,
        spaceAfter=8, textColor=colors.HexColor("#243247"),
    ))
    styles.add(ParagraphStyle(
        name="GuideSection", fontName="Helvetica-Bold", fontSize=18, leading=23,
        spaceBefore=15, spaceAfter=14, textColor=colors.HexColor("#25215b"), keepWithNext=True,
    ))
    styles.add(ParagraphStyle(
        name="GuideCell", fontName="Helvetica", fontSize=8, leading=11,
        spaceAfter=0, alignment=TA_LEFT,
    ))
    styles.add(ParagraphStyle(
        name="GuideCode", fontName="Courier", fontSize=7.7, leading=10.8,
        spaceBefore=4, spaceAfter=12, leftIndent=7,
        textColor=colors.HexColor("#182a44"), backColor=colors.HexColor("#f1f4f8"),
        borderPadding=8,
    ))
    title_style = ParagraphStyle(
        "CoverTitle", fontName="Helvetica-Bold", fontSize=32, leading=38,
        textColor=colors.HexColor("#25215b"), spaceAfter=18,
    )
    subtitle_style = ParagraphStyle(
        "CoverSubtitle", parent=styles["BodyGuide"], fontSize=19, leading=26, spaceAfter=22,
    )
    story = [
        Spacer(1, 24 * mm), Paragraph("GovBills", title_style),
        Paragraph("On-site deployment,<br/>code, dependencies<br/>and security", subtitle_style),
        HRFlowable(width="100%", thickness=3, color=colors.HexColor("#6554c0")),
        Spacer(1, 10 * mm),
        Paragraph("Technical implementation guide · 3 November 2026", styles["BodyGuide"]),
        Paragraph("Standalone Django portal · govbills-mauritius branch", styles["BodyGuide"]),
        Spacer(1, 7 * mm),
        Paragraph(
            "<b>Deployment and security review required.</b><br/>"
            "This is a source-based implementation guide, not an installed-system certificate. "
            "Temporary demo authentication and simulated financial examples must not be treated "
            "as approved live-operation controls or bank evidence.",
            styles["BodyGuide"],
        ),
        Spacer(1, 10 * mm),
        Paragraph("Contents", styles["Heading2"]),
    ]
    headings = [line[3:] for line in lines if re.match(r"## \d+\.", line)]
    for heading in headings:
        story.append(Paragraph(escape(heading), styles["BodyGuide"]))
    story.append(PageBreak())
    position = 2
    first_section = True
    while position < len(lines):
        line = lines[position].strip()
        if not line:
            position += 1
            continue
        if line.startswith("## "):
            if not first_section:
                story.append(CondPageBreak(130))
            first_section = False
            story.append(Paragraph(escape(line[3:]), styles["GuideSection"]))
            position += 1
        elif line == "```":
            block = []
            position += 1
            while position < len(lines) and lines[position].strip() != "```":
                block.append(lines[position])
                position += 1
            story.append(Preformatted("\n".join(block), styles["GuideCode"]))
            position += 1
        elif line.startswith("|"):
            rows = []
            while position < len(lines) and lines[position].strip().startswith("|"):
                row = [cell.strip() for cell in lines[position].strip().strip("|").split("|")]
                if not all(re.fullmatch(r"[-: ]+", cell or "-") for cell in row):
                    rows.append([Paragraph(escape(cell), styles["GuideCell"]) for cell in row])
                position += 1
            count = len(rows[0])
            widths = [49 * mm, 115 * mm] if count == 2 else [37 * mm, 62 * mm, 65 * mm]
            table = LongTable(rows, colWidths=widths, repeatRows=1, hAlign="LEFT")
            table.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e9e6f5")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 7),
                ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                ("LINEBELOW", (0, 0), (-1, -1), 0.4, colors.HexColor("#d7dce4")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f7f8fa")]),
            ]))
            story += [table, Spacer(1, 10)]
        elif line.startswith("- "):
            story.append(Paragraph("• " + escape(line[2:]), styles["BodyGuide"]))
            position += 1
        else:
            paragraph = [line]
            position += 1
            while position < len(lines) and lines[position].strip() and not lines[position].startswith(("## ", "|", "```", "- ")):
                paragraph.append(lines[position].strip())
                position += 1
            story.append(Paragraph(escape(" ".join(paragraph)), styles["BodyGuide"]))

    def page_decoration(canvas, document):
        canvas.saveState()
        width, height = A4
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#657086"))
        if document.page > 1:
            canvas.drawString(23 * mm, height - 14 * mm, "GOVBILLS  /  ON-SITE IMPLEMENTATION GUIDE")
            canvas.setStrokeColor(colors.HexColor("#d9dde5"))
            canvas.line(23 * mm, height - 17 * mm, width - 23 * mm, height - 17 * mm)
        canvas.drawString(23 * mm, 13 * mm, "Source-based guidance · 3 November 2026 · No credentials included")
        canvas.drawRightString(width - 23 * mm, 13 * mm, str(document.page))
        canvas.restoreState()

    document = SimpleDocTemplate(
        str(args.output), pagesize=A4, rightMargin=23 * mm, leftMargin=23 * mm,
        topMargin=24 * mm, bottomMargin=23 * mm,
        title="GovBills — On-site Deployment, Code, Dependencies and Security",
        author="GovBills implementation documentation",
    )
    document.build(story, onFirstPage=page_decoration, onLaterPages=page_decoration)
    print(args.output)


if __name__ == "__main__":
    main()
