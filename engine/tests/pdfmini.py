"""Tiny PDF writer for tests: one ruled table per page (pdfplumber finds tables from the ruling lines)."""


def table_pdf(path, pages: list[list[list[str]]], col_w: float = 90, row_h: float = 18) -> None:
    objs, kids = [], []
    font_id = 3

    def add(body: str) -> int:
        objs.append(body)
        return len(objs)

    add("<< /Type /Catalog /Pages 2 0 R >>")
    add("PAGES")                                             # placeholder, filled below
    add("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for rows in pages:
        ops = ["0.5 w"]
        top = 800
        for r, row in enumerate(rows):
            y = top - (r + 1) * row_h
            for c, txt in enumerate(row):
                x = 30 + c * col_w
                ops.append(f"{x} {y} {col_w} {row_h} re S")
                t = str(txt).replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
                ops.append(f"BT /F1 7 Tf {x + 2} {y + 6} Td ({t}) Tj ET")
        stream = "\n".join(ops)
        cid = add(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
        kids.append(add(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {60 + col_w * max(len(r) for r in rows)} 842] "
                        f"/Resources << /Font << /F1 {font_id} 0 R >> >> /Contents {cid} 0 R >>"))
    objs[1] = f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] /Count {len(kids)} >>"
    out, offsets = "%PDF-1.4\n", []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out.encode("latin-1")))
        out += f"{i} 0 obj\n{body}\nendobj\n"
    xref = len(out.encode("latin-1"))
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n" + "".join(f"{o:010d} 00000 n \n" for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    with open(path, "wb") as f:
        f.write(out.encode("latin-1"))
