"""PPTX read-only handler.

Scope: extract text from each slide. One Heading 1 per slide (slide title or
"Slide N"), followed by Paragraph blocks for each text frame's body.

Writing PPTX is deferred for v1 — slide layouts/masters/themes are a much
larger surface than a read-only path.

PPTX layout:
  /ppt/presentation.xml             — slide ordering via sldIdLst
  /ppt/_rels/presentation.xml.rels  — rId -> slide path
  /ppt/slides/slide1.xml ...        — actual text frames
"""
from __future__ import annotations
import zipfile
from pathlib import Path
from typing import List
from xml.etree import ElementTree as ET

from ..core.intermediate import Heading, Paragraph, Run, TextDoc
from ..utils.cancellation import CancellationToken

SUPPORTED_READ = {".pptx"}
SUPPORTED_WRITE: set[str] = set()
DOC_KIND = "text"


NS_P = "http://schemas.openxmlformats.org/presentationml/2006/main"
NS_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def read(path: Path, ext: str, cancel: CancellationToken) -> TextDoc:
    with zipfile.ZipFile(path) as z:
        cancel.check()
        try:
            pres_xml = z.read("ppt/presentation.xml")
            rels_xml = z.read("ppt/_rels/presentation.xml.rels")
        except KeyError:
            raise RuntimeError("Not a valid PPTX (missing presentation.xml).")
        slide_paths = _slide_paths_in_order(pres_xml, rels_xml)
        blocks = []
        for i, sp in enumerate(slide_paths, 1):
            cancel.check()
            full = sp if sp.startswith("ppt/") else f"ppt/{sp.lstrip('/')}"
            try:
                slide_xml = z.read(full)
            except KeyError:
                continue
            title, paras = _slide_text(slide_xml)
            blocks.append(Heading(level=1, runs=[Run(text=title or f"Slide {i}")]))
            for p in paras:
                if p.strip():
                    blocks.append(Paragraph(runs=[Run(text=p)]))
    return TextDoc(blocks=blocks)


def write(doc, path: Path, ext: str, cancel: CancellationToken) -> None:  # pragma: no cover
    raise RuntimeError("Writing PPTX is not supported in v1.")


def _slide_paths_in_order(pres_xml: bytes, rels_xml: bytes) -> List[str]:
    rels: dict[str, str] = {}
    try:
        root = ET.fromstring(rels_xml)
        for rel in root:
            rid = rel.get("Id")
            target = rel.get("Target")
            if rid and target:
                rels[rid] = target
    except ET.ParseError:
        pass

    paths: List[str] = []
    try:
        root = ET.fromstring(pres_xml)
    except ET.ParseError:
        return paths
    for sld_id in root.iter():
        if sld_id.tag.endswith("sldId"):
            rid = sld_id.get(f"{{{NS_R}}}id")
            if rid and rid in rels:
                paths.append(rels[rid])
    return paths


def _slide_text(slide_xml: bytes) -> tuple[str, List[str]]:
    """Returns (slide_title, list_of_paragraphs). Title heuristic: first <p:sp>
    whose <p:nvSpPr>/<p:nvPr>/<p:ph type="title|ctrTitle"> is set, OR the first
    text-bearing shape on the slide."""
    try:
        root = ET.fromstring(slide_xml)
    except ET.ParseError:
        return "", []
    title = ""
    paragraphs: List[str] = []
    for sp in root.iter(f"{{{NS_P}}}sp"):
        ph = None
        for nv in sp.iter(f"{{{NS_P}}}ph"):
            ph = nv
            break
        is_title = ph is not None and (ph.get("type") in ("title", "ctrTitle"))
        sp_text: List[str] = []
        for p in sp.iter(f"{{{NS_A}}}p"):
            run_text = []
            for t in p.iter(f"{{{NS_A}}}t"):
                if t.text:
                    run_text.append(t.text)
            line = "".join(run_text)
            if line:
                sp_text.append(line)
        if is_title and not title and sp_text:
            title = " ".join(sp_text)
        else:
            paragraphs.extend(sp_text)
    return title, paragraphs
