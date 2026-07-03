#!/usr/bin/env python3
"""
make_viewer.py
==============
Turn a folder of generated C-CDA XML files into a human-viewable folder.

For every *.xml under --input it produces, under --output (mirroring the folder
structure):

  * <name>.xml   -- a copy with an <?xml-stylesheet ...?> instruction attached,
                    pointing at the ccda.xsl dropped alongside it (so it renders
                    live when served over HTTP or opened in Firefox);
  * <name>.html  -- a standalone HTML rendering produced by applying that same
                    ccda.xsl (via lxml when available, else a stdlib fallback),
                    so you can just double-click it in any browser;
  * ccda.xsl     -- the stylesheet, written once per folder.

An index.html at the output root links every rendered document.

Usage:
    python3 make_viewer.py --input ./output --output ./viewable-output
    # then open ./viewable-output/index.html
"""

import argparse
import html as _html
import re
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

V3 = "urn:hl7-org:v3"

# --------------------------------------------------------------------------- #
# The C-CDA -> HTML stylesheet (XSLT 1.0). It renders the document header
# (patient / author / custodian / doc metadata) and each section's title plus
# its human-readable <text> narrative (tables, paragraphs, captions, lists).
# All human-readable content in these C-CDAs lives in section <text> blocks, so
# this faithfully shows everything a clinician would read.
# --------------------------------------------------------------------------- #
XSL = r"""<?xml version="1.0" encoding="UTF-8"?>
<xsl:stylesheet version="1.0"
    xmlns:xsl="http://www.w3.org/1999/XSL/Transform"
    xmlns:v3="urn:hl7-org:v3"
    exclude-result-prefixes="v3">
  <xsl:output method="html" indent="yes" encoding="UTF-8"
              doctype-system="about:legacy-compat"/>

  <!-- YYYYMMDD[hhmmss] -> YYYY-MM-DD [hh:mm] -->
  <xsl:template name="fmtdate">
    <xsl:param name="v"/>
    <xsl:choose>
      <xsl:when test="string-length($v) &gt;= 8">
        <xsl:value-of select="substring($v,1,4)"/>-<xsl:value-of select="substring($v,5,2)"/>-<xsl:value-of select="substring($v,7,2)"/>
        <xsl:if test="string-length($v) &gt;= 12">
          <xsl:text> </xsl:text>
          <xsl:value-of select="substring($v,9,2)"/>:<xsl:value-of select="substring($v,11,2)"/>
        </xsl:if>
      </xsl:when>
      <xsl:otherwise><xsl:value-of select="$v"/></xsl:otherwise>
    </xsl:choose>
  </xsl:template>

  <xsl:template match="/v3:ClinicalDocument">
    <html lang="en">
      <head>
        <meta charset="UTF-8"/>
        <meta name="viewport" content="width=device-width, initial-scale=1"/>
        <title><xsl:value-of select="v3:title"/></title>
        <style>
          :root{--ink:#1a2430;--muted:#5b6b7b;--line:#d9e1e8;--accent:#0b6b6b;--bg:#eef2f5;--card:#fff;}
          *{box-sizing:border-box;}
          body{margin:0;font:15px/1.55 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:var(--ink);background:var(--bg);}
          .wrap{max-width:1040px;margin:0 auto;padding:24px 20px 64px;}
          .doc-title{font-size:22px;font-weight:700;margin:0 0 2px;}
          .doc-sub{color:var(--muted);font-size:13px;margin-bottom:18px;}
          .banner{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px 18px;margin-bottom:22px;}
          .banner h1{margin:0 0 10px;font-size:19px;}
          .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:6px 22px;}
          .kv{font-size:13.5px;}
          .kv .k{color:var(--muted);margin-right:6px;}
          .toc{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 18px;margin-bottom:22px;}
          .toc a{color:var(--accent);text-decoration:none;margin-right:14px;font-size:13.5px;white-space:nowrap;}
          .toc a:hover{text-decoration:underline;}
          .section{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:6px 20px 18px;margin-bottom:18px;}
          .section h2{font-size:16px;color:var(--accent);border-bottom:2px solid var(--line);padding-bottom:8px;margin:14px 0 12px;}
          table{border-collapse:collapse;width:100%;margin:10px 0;font-size:13.5px;}
          th,td{border:1px solid var(--line);padding:7px 9px;text-align:left;vertical-align:top;}
          thead th{background:#f3f7f8;color:var(--ink);font-weight:600;}
          tbody tr:nth-child(even){background:#fafcfc;}
          .para{margin:10px 0;padding:10px 12px;background:#f7fafb;border-left:3px solid var(--accent);border-radius:0 6px 6px 0;}
          .caption{font-weight:600;color:var(--ink);margin-bottom:4px;}
          .content{display:block;}
          .empty{color:var(--muted);font-style:italic;}
          footer{color:var(--muted);font-size:12px;text-align:center;margin-top:26px;}
        </style>
      </head>
      <body>
        <div class="wrap">
          <div class="doc-title"><xsl:value-of select="v3:title"/></div>
          <div class="doc-sub">
            <xsl:value-of select="v3:code/@displayName"/>
            <xsl:text> &#183; effective </xsl:text>
            <xsl:call-template name="fmtdate"><xsl:with-param name="v" select="v3:effectiveTime/@value"/></xsl:call-template>
          </div>

          <!-- Patient banner -->
          <xsl:variable name="p" select="v3:recordTarget/v3:patientRole"/>
          <div class="banner">
            <h1>
              <xsl:value-of select="$p/v3:patient/v3:name/v3:given"/>
              <xsl:text> </xsl:text>
              <xsl:value-of select="$p/v3:patient/v3:name/v3:family"/>
            </h1>
            <div class="grid">
              <div class="kv"><span class="k">MRN:</span><xsl:value-of select="$p/v3:id/@extension"/></div>
              <div class="kv"><span class="k">Sex:</span><xsl:value-of select="$p/v3:patient/v3:administrativeGenderCode/@code"/></div>
              <div class="kv"><span class="k">DOB:</span>
                <xsl:call-template name="fmtdate"><xsl:with-param name="v" select="$p/v3:patient/v3:birthTime/@value"/></xsl:call-template>
              </div>
              <div class="kv"><span class="k">Author:</span>
                <xsl:value-of select="v3:author/v3:assignedAuthor/v3:assignedPerson/v3:name/v3:given"/>
                <xsl:text> </xsl:text>
                <xsl:value-of select="v3:author/v3:assignedAuthor/v3:assignedPerson/v3:name/v3:family"/>
              </div>
              <div class="kv"><span class="k">Custodian:</span>
                <xsl:value-of select="v3:custodian/v3:assignedCustodian/v3:representedCustodianOrganization/v3:name"/>
              </div>
            </div>
          </div>

          <!-- Table of contents -->
          <div class="toc">
            <xsl:for-each select="v3:component/v3:structuredBody/v3:component/v3:section">
              <a href="#sec{position()}"><xsl:value-of select="v3:title"/></a>
            </xsl:for-each>
          </div>

          <!-- Sections -->
          <xsl:for-each select="v3:component/v3:structuredBody/v3:component/v3:section">
            <div class="section" id="sec{position()}">
              <h2><xsl:value-of select="v3:title"/></h2>
              <xsl:apply-templates select="v3:text"/>
            </div>
          </xsl:for-each>

          <footer>Rendered from C-CDA by make_viewer.py</footer>
        </div>
      </body>
    </html>
  </xsl:template>

  <!-- Narrative block rendering -->
  <xsl:template match="v3:text"><xsl:apply-templates/></xsl:template>
  <xsl:template match="v3:table">
    <table>
      <xsl:if test="@border"><xsl:attribute name="border"><xsl:value-of select="@border"/></xsl:attribute></xsl:if>
      <xsl:apply-templates/>
    </table>
  </xsl:template>
  <xsl:template match="v3:thead"><thead><xsl:apply-templates/></thead></xsl:template>
  <xsl:template match="v3:tbody"><tbody><xsl:apply-templates/></tbody></xsl:template>
  <xsl:template match="v3:tr"><tr><xsl:apply-templates/></tr></xsl:template>
  <xsl:template match="v3:th"><th><xsl:apply-templates/></th></xsl:template>
  <xsl:template match="v3:td"><td><xsl:apply-templates/></td></xsl:template>
  <xsl:template match="v3:paragraph">
    <div class="para"><xsl:apply-templates/></div>
  </xsl:template>
  <xsl:template match="v3:caption"><div class="caption"><xsl:apply-templates/></div></xsl:template>
  <xsl:template match="v3:content"><span class="content"><xsl:apply-templates/></span></xsl:template>
  <xsl:template match="v3:list"><ul><xsl:apply-templates/></ul></xsl:template>
  <xsl:template match="v3:item"><li><xsl:apply-templates/></li></xsl:template>
  <xsl:template match="v3:br"><br/></xsl:template>
  <xsl:template match="v3:renderMultiMedia"/>
  <xsl:template match="text()"><xsl:value-of select="."/></xsl:template>
</xsl:stylesheet>
"""

STYLESHEET_PI = '<?xml-stylesheet type="text/xsl" href="ccda.xsl"?>'


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def render_with_lxml(xml_path):
    from lxml import etree
    xslt = etree.XSLT(etree.fromstring(XSL.encode("utf-8")))
    result = xslt(etree.parse(str(xml_path)))
    return str(result)


def _q(tag):
    return f"{{{V3}}}{tag}"


def _render_narrative_et(el, parts):
    """Minimal stdlib fallback: convert a section <text> subtree to HTML."""
    tag = el.tag.split("}")[-1]
    simple = {"table": "table", "thead": "thead", "tbody": "tbody", "tr": "tr",
              "th": "th", "td": "td", "list": "ul", "item": "li", "br": "br"}
    if tag == "paragraph":
        parts.append('<div class="para">')
    elif tag == "caption":
        parts.append('<div class="caption">')
    elif tag == "content":
        parts.append('<span class="content">')
    elif tag in simple:
        parts.append(f"<{simple[tag]}>")
    if el.text:
        parts.append(_html.escape(el.text))
    for child in el:
        _render_narrative_et(child, parts)
        if child.tail:
            parts.append(_html.escape(child.tail))
    if tag == "paragraph" or tag == "caption":
        parts.append("</div>")
    elif tag == "content":
        parts.append("</span>")
    elif tag in simple and tag != "br":
        parts.append(f"</{simple[tag]}>")


def render_with_stdlib(xml_path):
    root = ET.parse(str(xml_path)).getroot()
    def txt(path):
        e = root.find(path)
        return e.text if e is not None and e.text else ""
    def attr(path, a):
        e = root.find(path)
        return e.get(a, "") if e is not None else ""
    pr = "{ns}recordTarget/{ns}patientRole".format(ns=f"{{{V3}}}")
    given = txt(f"{pr}/{_q('patient')}/{_q('name')}/{_q('given')}")
    family = txt(f"{pr}/{_q('patient')}/{_q('name')}/{_q('family')}")
    secs = []
    for comp in root.findall(f"{_q('component')}/{_q('structuredBody')}/{_q('component')}/{_q('section')}"):
        title = comp.findtext(_q("title")) or "Section"
        body = []
        t = comp.find(_q("text"))
        if t is not None:
            for child in t:
                _render_narrative_et(child, body)
                if child.tail:
                    body.append(_html.escape(child.tail))
        secs.append((title, "".join(body)))
    toc = " ".join(f'<a href="#sec{i}">{_html.escape(t)}</a>' for i, (t, _) in enumerate(secs, 1))
    body_html = "".join(
        f'<div class="section" id="sec{i}"><h2>{_html.escape(t)}</h2>{b}</div>'
        for i, (t, b) in enumerate(secs, 1))
    doc_title = txt(_q("title"))
    return f"""<!doctype html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{_html.escape(doc_title)}</title>
<style>{_FALLBACK_CSS}</style></head><body><div class="wrap">
<div class="doc-title">{_html.escape(doc_title)}</div>
<div class="banner"><h1>{_html.escape(given)} {_html.escape(family)}</h1></div>
<div class="toc">{toc}</div>{body_html}
<footer>Rendered from C-CDA by make_viewer.py (stdlib fallback)</footer></div></body></html>"""


_FALLBACK_CSS = ("body{margin:0;font:15px/1.55 -apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#1a2430;background:#eef2f5;}"
                 ".wrap{max-width:1040px;margin:0 auto;padding:24px 20px 64px;}"
                 ".doc-title{font-size:22px;font-weight:700;margin-bottom:16px;}"
                 ".banner,.section,.toc{background:#fff;border:1px solid #d9e1e8;border-radius:10px;padding:14px 20px;margin-bottom:18px;}"
                 ".section h2{font-size:16px;color:#0b6b6b;border-bottom:2px solid #d9e1e8;padding-bottom:8px;}"
                 "table{border-collapse:collapse;width:100%;margin:10px 0;font-size:13.5px;}"
                 "th,td{border:1px solid #d9e1e8;padding:7px 9px;text-align:left;vertical-align:top;}"
                 "thead th{background:#f3f7f8;}"
                 ".para{margin:10px 0;padding:10px 12px;background:#f7fafb;border-left:3px solid #0b6b6b;}"
                 ".caption{font-weight:600;margin-bottom:4px;}.content{display:block;}"
                 ".toc a{color:#0b6b6b;text-decoration:none;margin-right:14px;}"
                 "footer{color:#5b6b7b;font-size:12px;text-align:center;margin-top:26px;}")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def attach_stylesheet(xml_text):
    """Insert the <?xml-stylesheet?> PI right after the XML declaration."""
    m = re.match(r"^(<\?xml[^>]*\?>\s*)", xml_text)
    if m:
        return m.group(1) + STYLESHEET_PI + "\n" + xml_text[m.end():]
    return STYLESHEET_PI + "\n" + xml_text


def label_for(path):
    """Human label from a filename like ABILENE_20240111_22.xml."""
    stem = path.stem
    m = re.search(r"_(\d{8})_", stem)
    if m:
        d = m.group(1)
        return f"Visit {d[:4]}-{d[4:6]}-{d[6:8]}"
    if "summary" in stem.lower():
        return "Patient Summary"
    return stem


def main():
    ap = argparse.ArgumentParser(description="Render C-CDA XML into a viewable HTML folder.")
    ap.add_argument("--input", default="./output", help="folder containing generated C-CDA .xml files")
    ap.add_argument("--output", default="./viewable-output", help="folder to write the viewer into")
    args = ap.parse_args()

    in_root = Path(args.input)
    out_root = Path(args.output)
    if not in_root.is_dir():
        sys.exit(f"Input directory not found: {in_root}")

    xmls = sorted(p for p in in_root.rglob("*.xml"))
    if not xmls:
        sys.exit(f"No .xml files found under {in_root}")

    try:
        import lxml.etree  # noqa: F401
        use_lxml = True
    except ImportError:
        use_lxml = False
    render = render_with_lxml if use_lxml else render_with_stdlib

    out_root.mkdir(parents=True, exist_ok=True)
    xsl_dirs = set()
    index_rows = []
    ok = fail = 0
    for xml in xmls:
        rel = xml.relative_to(in_root)
        dest_dir = out_root / rel.parent
        dest_dir.mkdir(parents=True, exist_ok=True)
        # ccda.xsl once per folder that holds documents
        if dest_dir not in xsl_dirs:
            (dest_dir / "ccda.xsl").write_text(XSL, encoding="utf-8")
            xsl_dirs.add(dest_dir)
        try:
            # 1) xml copy with stylesheet PI attached
            (dest_dir / xml.name).write_text(
                attach_stylesheet(xml.read_text(encoding="utf-8")), encoding="utf-8")
            # 2) standalone rendered html
            html_path = dest_dir / (xml.stem + ".html")
            html_path.write_text(render(xml), encoding="utf-8")
            index_rows.append((rel.parent.as_posix(), label_for(xml),
                               (html_path.relative_to(out_root)).as_posix()))
            ok += 1
        except Exception as e:  # noqa: BLE001
            print(f"  ERR {xml}: {e}")
            fail += 1

    # index.html
    index_rows.sort(key=lambda r: (r[0], r[1]))
    links = "\n".join(
        f'<li><a href="{_html.escape(href)}">{_html.escape(label)}</a>'
        f'<span class="folder">{_html.escape(folder)}</span></li>'
        for folder, label, href in index_rows)
    (out_root / "index.html").write_text(f"""<!doctype html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>C-CDA Viewer</title>
<style>{_FALLBACK_CSS} ul{{list-style:none;padding:0;}} li{{padding:8px 12px;border-bottom:1px solid #eef2f5;display:flex;justify-content:space-between;}}
li a{{color:#0b6b6b;text-decoration:none;font-weight:600;}} .folder{{color:#5b6b7b;font-size:12px;}}</style></head>
<body><div class="wrap"><div class="doc-title">C-CDA Documents ({ok})</div>
<div class="section"><ul>{links}</ul></div>
<footer>Open any document above. Rendered by make_viewer.py{' (lxml + ccda.xsl)' if use_lxml else ' (stdlib fallback)'}.</footer>
</div></body></html>""", encoding="utf-8")

    print(f"Rendered {ok} document(s){' , '+str(fail)+' failed' if fail else ''} using "
          f"{'lxml + ccda.xsl' if use_lxml else 'stdlib fallback'}.")
    print(f"Open: {out_root / 'index.html'}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
