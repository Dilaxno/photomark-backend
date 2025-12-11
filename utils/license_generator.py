"""
Commercial License Generator for Shop Digital Products

Generates professional PDF licenses similar to Shutterstock/iStock
for buyers of digital products from public shops.
"""

import os
import uuid
import hashlib
from datetime import datetime
from typing import Optional, List, Dict, Any
from io import BytesIO

# Try to import reportlab for PDF generation
try:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter, A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image, HRFlowable
    from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
    REPORTLAB_AVAILABLE = True
except ImportError:
    REPORTLAB_AVAILABLE = False

from core.config import logger

APP_NAME = os.getenv("APP_NAME", "Photomark")
COMPANY_NAME = os.getenv("COMPANY_NAME", "Photomark Inc.")
COMPANY_ADDRESS = os.getenv("COMPANY_ADDRESS", "")
COMPANY_WEBSITE = os.getenv("COMPANY_WEBSITE", "https://photomark.cloud")


def generate_license_number(payment_id: str, item_id: str, timestamp: datetime) -> str:
    """Generate a unique license number based on payment and item details."""
    seed = f"{payment_id}-{item_id}-{timestamp.isoformat()}"
    hash_part = hashlib.sha256(seed.encode()).hexdigest()[:8].upper()
    date_part = timestamp.strftime("%Y%m%d")
    return f"LIC-{date_part}-{hash_part}"


def generate_license_pdf(
    license_number: str,
    buyer_name: str,
    buyer_email: str,
    seller_name: str,
    shop_name: str,
    items: List[Dict[str, Any]],
    payment_id: str,
    purchase_date: datetime,
    total_amount: float,
    currency: str = "USD",
    license_type: str = "Standard Commercial License",
) -> bytes:
    """
    Generate a professional PDF commercial license.
    
    Returns the PDF as bytes.
    """
    if not REPORTLAB_AVAILABLE:
        # Fallback to HTML-based license if reportlab not available
        return generate_license_html_pdf(
            license_number, buyer_name, buyer_email, seller_name, shop_name,
            items, payment_id, purchase_date, total_amount, currency, license_type
        )
    
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        rightMargin=0.75*inch,
        leftMargin=0.75*inch,
        topMargin=0.75*inch,
        bottomMargin=0.75*inch
    )
    
    styles = getSampleStyleSheet()
    
    # Custom styles
    title_style = ParagraphStyle(
        'CustomTitle',
        parent=styles['Heading1'],
        fontSize=24,
        spaceAfter=6,
        alignment=TA_CENTER,
        textColor=colors.HexColor('#1a1a2e'),
        fontName='Helvetica-Bold'
    )
    
    subtitle_style = ParagraphStyle(
        'CustomSubtitle',
        parent=styles['Normal'],
        fontSize=12,
        spaceAfter=20,
        alignment=TA_CENTER,
        textColor=colors.HexColor('#666666')
    )
    
    section_header_style = ParagraphStyle(
        'SectionHeader',
        parent=styles['Heading2'],
        fontSize=14,
        spaceBefore=16,
        spaceAfter=8,
        textColor=colors.HexColor('#1a1a2e'),
        fontName='Helvetica-Bold'
    )
    
    body_style = ParagraphStyle(
        'CustomBody',
        parent=styles['Normal'],
        fontSize=10,
        spaceAfter=6,
        textColor=colors.HexColor('#333333'),
        leading=14
    )
    
    small_style = ParagraphStyle(
        'SmallText',
        parent=styles['Normal'],
        fontSize=8,
        textColor=colors.HexColor('#888888'),
        alignment=TA_CENTER
    )
    
    story = []
    
    # Header
    story.append(Paragraph("COMMERCIAL LICENSE CERTIFICATE", title_style))
    story.append(Paragraph(f"License #{license_number}", subtitle_style))
    story.append(HRFlowable(width="100%", thickness=2, color=colors.HexColor('#7AA2F7'), spaceBefore=0, spaceAfter=20))
    
    # License Details Table
    story.append(Paragraph("LICENSE DETAILS", section_header_style))
    
    license_data = [
        ["License Number:", license_number],
        ["License Type:", license_type],
        ["Issue Date:", purchase_date.strftime("%B %d, %Y")],
        ["Valid From:", purchase_date.strftime("%B %d, %Y")],
        ["Validity:", "Perpetual (Non-Expiring)"],
    ]
    
    license_table = Table(license_data, colWidths=[2*inch, 4.5*inch])
    license_table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
        ('FONTNAME', (1, 0), (1, -1), 'Helvetica'),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('TEXTCOLOR', (0, 0), (0, -1), colors.HexColor('#666666')),
        ('TEXTCOLOR', (1, 0), (1, -1), colors.HexColor('#1a1a2e')),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('ALIGN', (0, 0), (0, -1), 'RIGHT'),
        ('ALIGN', (1, 0), (1, -1), 'LEFT'),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
    ]))
    story.append(license_table)
    story.append(Spacer(1, 16))
    
    # Licensee Information
    story.append(Paragraph("LICENSEE INFORMATION", section_header_style))
    
    licensee_data = [
        ["Licensed To:", buyer_name or buyer_email],
        ["Email:", buyer_email],
        ["Transaction ID:", payment_id or "N/A"],
    ]
    
    licensee_table = Table(licensee_data, colWidths=[2*inch, 4.5*inch])
    licensee_table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
        ('FONTNAME', (1, 0), (1, -1), 'Helvetica'),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('TEXTCOLOR', (0, 0), (0, -1), colors.HexColor('#666666')),
        ('TEXTCOLOR', (1, 0), (1, -1), colors.HexColor('#1a1a2e')),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('ALIGN', (0, 0), (0, -1), 'RIGHT'),
        ('ALIGN', (1, 0), (1, -1), 'LEFT'),
    ]))
    story.append(licensee_table)
    story.append(Spacer(1, 16))
    
    # Licensor Information
    story.append(Paragraph("LICENSOR INFORMATION", section_header_style))
    
    licensor_data = [
        ["Seller:", seller_name or shop_name],
        ["Shop:", shop_name],
        ["Platform:", APP_NAME],
    ]
    
    licensor_table = Table(licensor_data, colWidths=[2*inch, 4.5*inch])
    licensor_table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
        ('FONTNAME', (1, 0), (1, -1), 'Helvetica'),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('TEXTCOLOR', (0, 0), (0, -1), colors.HexColor('#666666')),
        ('TEXTCOLOR', (1, 0), (1, -1), colors.HexColor('#1a1a2e')),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('ALIGN', (0, 0), (0, -1), 'RIGHT'),
        ('ALIGN', (1, 0), (1, -1), 'LEFT'),
    ]))
    story.append(licensor_table)
    story.append(Spacer(1, 16))
    
    # Licensed Items
    story.append(Paragraph("LICENSED ITEMS", section_header_style))
    
    items_header = [["#", "Item", "Price"]]
    items_data = []
    for idx, item in enumerate(items, 1):
        title = item.get("title", "Digital Product")
        price = item.get("price", 0)
        if isinstance(price, (int, float)):
            price_str = f"${price/100:.2f}" if price > 100 else f"${price:.2f}"
        else:
            price_str = str(price)
        items_data.append([str(idx), title, price_str])
    
    items_data.append(["", "Total:", f"${total_amount:.2f} {currency}"])
    
    items_table = Table(items_header + items_data, colWidths=[0.5*inch, 4.5*inch, 1.5*inch])
    items_table.setStyle(TableStyle([
        # Header
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#f0f0f0')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.HexColor('#1a1a2e')),
        # Body
        ('FONTNAME', (0, 1), (-1, -2), 'Helvetica'),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        # Total row
        ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
        ('LINEABOVE', (0, -1), (-1, -1), 1, colors.HexColor('#cccccc')),
        # General
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ('TOPPADDING', (0, 0), (-1, -1), 8),
        ('ALIGN', (0, 0), (0, -1), 'CENTER'),
        ('ALIGN', (2, 0), (2, -1), 'RIGHT'),
        ('GRID', (0, 0), (-1, -2), 0.5, colors.HexColor('#e0e0e0')),
    ]))
    story.append(items_table)
    story.append(Spacer(1, 20))
    
    # License Terms
    story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor('#e0e0e0'), spaceBefore=10, spaceAfter=10))
    story.append(Paragraph("LICENSE GRANT & PERMITTED USES", section_header_style))
    
    terms = [
        "This license grants the Licensee a non-exclusive, worldwide, perpetual right to use the licensed digital content for commercial and personal purposes.",
        "",
        "<b>Permitted Uses:</b>",
        "• Use in commercial projects, advertisements, and marketing materials",
        "• Use in digital and print media, websites, and social media",
        "• Use in merchandise and products for sale (up to 500,000 copies)",
        "• Modification and derivative works creation",
        "• Use in multiple projects without additional licensing",
        "",
        "<b>Restrictions:</b>",
        "• Resale or redistribution of the original files is prohibited",
        "• Use in trademark or logo registration is prohibited",
        "• Sublicensing to third parties is prohibited",
        "• Use in defamatory, illegal, or immoral content is prohibited",
    ]
    
    for term in terms:
        if term:
            story.append(Paragraph(term, body_style))
        else:
            story.append(Spacer(1, 6))
    
    story.append(Spacer(1, 20))
    
    # Footer
    story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor('#e0e0e0'), spaceBefore=10, spaceAfter=10))
    
    footer_text = f"""
    This license certificate was automatically generated by {APP_NAME} on {datetime.utcnow().strftime("%B %d, %Y at %H:%M UTC")}.
    For verification or support, please contact the seller or visit {COMPANY_WEBSITE}.
    License verification code: {license_number}
    """
    story.append(Paragraph(footer_text, small_style))
    
    # Build PDF
    doc.build(story)
    
    pdf_bytes = buffer.getvalue()
    buffer.close()
    
    return pdf_bytes


def generate_license_html_pdf(
    license_number: str,
    buyer_name: str,
    buyer_email: str,
    seller_name: str,
    shop_name: str,
    items: List[Dict[str, Any]],
    payment_id: str,
    purchase_date: datetime,
    total_amount: float,
    currency: str = "USD",
    license_type: str = "Standard Commercial License",
) -> bytes:
    """
    Fallback: Generate license as HTML (can be converted to PDF client-side or printed).
    Returns HTML as UTF-8 bytes.
    """
    items_html = ""
    for idx, item in enumerate(items, 1):
        title = item.get("title", "Digital Product")
        price = item.get("price", 0)
        if isinstance(price, (int, float)):
            price_str = f"${price/100:.2f}" if price > 100 else f"${price:.2f}"
        else:
            price_str = str(price)
        items_html += f"<tr><td>{idx}</td><td>{title}</td><td style='text-align:right'>{price_str}</td></tr>"
    
    html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Commercial License - {license_number}</title>
    <style>
        @media print {{
            body {{ margin: 0; padding: 20px; }}
            .no-print {{ display: none; }}
        }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            max-width: 800px;
            margin: 0 auto;
            padding: 40px;
            color: #1a1a2e;
            line-height: 1.6;
        }}
        .header {{
            text-align: center;
            border-bottom: 3px solid #7AA2F7;
            padding-bottom: 20px;
            margin-bottom: 30px;
        }}
        .header h1 {{
            margin: 0;
            font-size: 28px;
            color: #1a1a2e;
        }}
        .header .license-num {{
            color: #666;
            font-size: 14px;
            margin-top: 8px;
        }}
        .section {{
            margin-bottom: 24px;
        }}
        .section h2 {{
            font-size: 14px;
            color: #7AA2F7;
            text-transform: uppercase;
            letter-spacing: 1px;
            margin-bottom: 12px;
            border-bottom: 1px solid #e0e0e0;
            padding-bottom: 8px;
        }}
        .info-grid {{
            display: grid;
            grid-template-columns: 150px 1fr;
            gap: 8px 16px;
        }}
        .info-label {{
            color: #666;
            font-weight: 500;
        }}
        .info-value {{
            color: #1a1a2e;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            margin-top: 12px;
        }}
        th, td {{
            padding: 10px;
            text-align: left;
            border-bottom: 1px solid #e0e0e0;
        }}
        th {{
            background: #f5f5f5;
            font-weight: 600;
        }}
        .total-row {{
            font-weight: bold;
            border-top: 2px solid #1a1a2e;
        }}
        .terms {{
            background: #f9f9f9;
            padding: 20px;
            border-radius: 8px;
            font-size: 13px;
        }}
        .terms h3 {{
            margin-top: 16px;
            margin-bottom: 8px;
            font-size: 13px;
        }}
        .terms ul {{
            margin: 0;
            padding-left: 20px;
        }}
        .terms li {{
            margin-bottom: 4px;
        }}
        .footer {{
            margin-top: 40px;
            padding-top: 20px;
            border-top: 1px solid #e0e0e0;
            text-align: center;
            font-size: 11px;
            color: #888;
        }}
        .print-btn {{
            display: block;
            margin: 20px auto;
            padding: 12px 24px;
            background: #7AA2F7;
            color: white;
            border: none;
            border-radius: 8px;
            font-size: 14px;
            cursor: pointer;
        }}
        .print-btn:hover {{
            background: #5a8af7;
        }}
    </style>
</head>
<body>
    <button class="print-btn no-print" onclick="window.print()">🖨️ Print / Save as PDF</button>
    
    <div class="header">
        <h1>COMMERCIAL LICENSE CERTIFICATE</h1>
        <div class="license-num">License #{license_number}</div>
    </div>
    
    <div class="section">
        <h2>License Details</h2>
        <div class="info-grid">
            <span class="info-label">License Number:</span>
            <span class="info-value">{license_number}</span>
            <span class="info-label">License Type:</span>
            <span class="info-value">{license_type}</span>
            <span class="info-label">Issue Date:</span>
            <span class="info-value">{purchase_date.strftime("%B %d, %Y")}</span>
            <span class="info-label">Validity:</span>
            <span class="info-value">Perpetual (Non-Expiring)</span>
        </div>
    </div>
    
    <div class="section">
        <h2>Licensee Information</h2>
        <div class="info-grid">
            <span class="info-label">Licensed To:</span>
            <span class="info-value">{buyer_name or buyer_email}</span>
            <span class="info-label">Email:</span>
            <span class="info-value">{buyer_email}</span>
            <span class="info-label">Transaction ID:</span>
            <span class="info-value">{payment_id or 'N/A'}</span>
        </div>
    </div>
    
    <div class="section">
        <h2>Licensor Information</h2>
        <div class="info-grid">
            <span class="info-label">Seller:</span>
            <span class="info-value">{seller_name or shop_name}</span>
            <span class="info-label">Shop:</span>
            <span class="info-value">{shop_name}</span>
            <span class="info-label">Platform:</span>
            <span class="info-value">{APP_NAME}</span>
        </div>
    </div>
    
    <div class="section">
        <h2>Licensed Items</h2>
        <table>
            <thead>
                <tr>
                    <th style="width:40px">#</th>
                    <th>Item</th>
                    <th style="width:100px;text-align:right">Price</th>
                </tr>
            </thead>
            <tbody>
                {items_html}
                <tr class="total-row">
                    <td></td>
                    <td>Total</td>
                    <td style="text-align:right">${total_amount:.2f} {currency}</td>
                </tr>
            </tbody>
        </table>
    </div>
    
    <div class="section terms">
        <h2 style="margin-top:0">License Grant & Permitted Uses</h2>
        <p>This license grants the Licensee a non-exclusive, worldwide, perpetual right to use the licensed digital content for commercial and personal purposes.</p>
        
        <h3>Permitted Uses:</h3>
        <ul>
            <li>Use in commercial projects, advertisements, and marketing materials</li>
            <li>Use in digital and print media, websites, and social media</li>
            <li>Use in merchandise and products for sale (up to 500,000 copies)</li>
            <li>Modification and derivative works creation</li>
            <li>Use in multiple projects without additional licensing</li>
        </ul>
        
        <h3>Restrictions:</h3>
        <ul>
            <li>Resale or redistribution of the original files is prohibited</li>
            <li>Use in trademark or logo registration is prohibited</li>
            <li>Sublicensing to third parties is prohibited</li>
            <li>Use in defamatory, illegal, or immoral content is prohibited</li>
        </ul>
    </div>
    
    <div class="footer">
        <p>This license certificate was automatically generated by {APP_NAME} on {datetime.utcnow().strftime("%B %d, %Y at %H:%M UTC")}.</p>
        <p>For verification or support, please contact the seller or visit {COMPANY_WEBSITE}.</p>
        <p>License verification code: {license_number}</p>
    </div>
</body>
</html>"""
    
    return html.encode('utf-8')


def generate_license_data(
    payment_id: str,
    buyer_name: str,
    buyer_email: str,
    seller_name: str,
    shop_name: str,
    items: List[Dict[str, Any]],
    purchase_date: Optional[datetime] = None,
    total_amount: float = 0,
    currency: str = "USD",
) -> Dict[str, Any]:
    """
    Generate license metadata (for storage/verification).
    """
    if purchase_date is None:
        purchase_date = datetime.utcnow()
    
    # Generate unique license numbers for each item
    licenses = []
    for item in items:
        item_id = item.get("id", str(uuid.uuid4())[:8])
        license_number = generate_license_number(payment_id, item_id, purchase_date)
        licenses.append({
            "license_number": license_number,
            "item_id": item_id,
            "item_title": item.get("title", "Digital Product"),
            "issued_at": purchase_date.isoformat(),
        })
    
    # Master license number for the entire purchase
    master_license = generate_license_number(payment_id, "master", purchase_date)
    
    return {
        "master_license_number": master_license,
        "payment_id": payment_id,
        "buyer_name": buyer_name,
        "buyer_email": buyer_email,
        "seller_name": seller_name,
        "shop_name": shop_name,
        "items": licenses,
        "total_amount": total_amount,
        "currency": currency,
        "issued_at": purchase_date.isoformat(),
        "license_type": "Standard Commercial License",
        "validity": "perpetual",
    }
