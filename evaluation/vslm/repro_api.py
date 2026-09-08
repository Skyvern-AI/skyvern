#!/usr/bin/env python3
"""
Reproduction: V-SLM drops sentences whose relevance is carried by an identifier.

    export ZQ_API_KEY=<your key>
    python3 zq_repro_identifier_recall.py

Standalone -- only needs Python 3.8+ and the API key. No other dependencies.

Summary of what this shows
-------------------------
When a sentence states a labelled identifier ("Invoice #: 88213-B") and the topic
names that identifier ("invoice number"), the sentence is classified irrelevant,
often at very low probability. The same fact written as a narrative sentence
("Account number 4471-99823-01 is enrolled in paperless billing.") is kept at
~0.99. Case C below is the sharpest: the topic string is a verbatim prefix of the
sentence and it still scores ~0.02.

This matters for document data-extraction: the fields users most want out of an
invoice, statement, or form are exactly the labelled identifiers, and dropping
them causes silent extraction failure (the field comes back null, no error).
"""

import json
import os
import sys
import urllib.error
import urllib.request

BASE_URL = os.environ.get("ZQ_BASE_URL", "https://api.zettaquant.ai")
API_KEY = os.environ.get("ZQ_API_KEY", "")

AGENTS = [
    "general_context_agent",
    "legal_context_agent",
    "reports_context_agent",
    "transcript_context_agent",
    "web_context_agent",
    "log_context_agent",
    "social_media_context_agent",
]

# (case id, topic, sentence, what we expect)
CASES = [
    ("A", "invoice number", "Invoice number INV-2026-04871 was issued on March 14, 2026.", "relevant"),
    ("B", "invoice number", "Invoice #: 88213-B", "relevant"),
    ("C", "order confirmation number", "Order confirmation number: 7731-AAQ-2026", "relevant"),
    ("D", "account number", "Acct No. 5590231", "relevant"),
    ("E", "policy number", "Policy number POL-33871-XA takes effect on the first of the month.", "relevant"),
    # Controls: same topics, narrative phrasing -- these are classified correctly.
    ("F", "account number", "Account number 4471-99823-01 is enrolled in paperless billing.", "relevant"),
    ("G", "shipping cost", "Standard shipping costs $6.99 for orders under $35.", "relevant"),
    ("H", "shipping cost", "The warehouse is located in Memphis.", "irrelevant"),
]


def predict(agent, sentences, topic=None, query=None):
    body = {"agent": agent, "sentences": sentences}
    if topic is not None:
        body["topic"] = topic
    if query is not None:
        body["query"] = query
    req = urllib.request.Request(
        f"{BASE_URL.rstrip('/')}/v1/vslm/predict",
        data=json.dumps(body).encode(),
        headers={"x-api-key": API_KEY, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)


def score_of(payload, sentence):
    for s in payload.get("scores", []):
        if s["sentence"] == sentence:
            return s["relevant_prob"], s["relevant"]
    return None, sentence in payload.get("relevant_sentences", [])


def main():
    if not API_KEY:
        sys.exit("set ZQ_API_KEY first")

    print("=" * 78)
    print("PART 1 -- identifier sentences vs narrative sentences (general_context_agent)")
    print("=" * 78)
    print(f"{'':3} {'topic':<27} {'prob':>7}  {'verdict':<9} sentence")
    wrong = []
    for cid, topic, sentence, expected in CASES:
        payload = predict("general_context_agent", [sentence], topic=topic)
        prob, kept = score_of(payload, sentence)
        verdict = "KEEP" if kept else "DROP"
        bad = (expected == "relevant") != kept
        if bad:
            wrong.append((cid, topic, sentence, prob))
        print(f"{cid:3} {topic:<27} {prob:>7}  {verdict:<9}{'  <-- WRONG' if bad else '':<12} {sentence[:52]}")

    print("\n" + "=" * 78)
    print("PART 2 -- case A across every topic-specification mode")
    print("=" * 78)
    sentence = CASES[0][2]
    modes = [
        ("explicit topic, exact field name", {"topic": "invoice number"}),
        ("explicit topic, natural phrasing", {"topic": "the invoice number and invoice identifier"}),
        ("schema-derived topic", {"topic": "invoice number, The invoice identifier, total amount due"}),
        ("query mode (ZQ generates topic)", {"query": "What is the invoice number on this invoice?"}),
    ]
    for label, kwargs in modes:
        payload = predict("general_context_agent", [sentence], **kwargs)
        prob, kept = score_of(payload, sentence)
        generated = payload.get("topic_used")
        print(f"  {label:<36} prob={prob:<8} {'KEEP' if kept else 'DROP'}")
        if kwargs.get("query"):
            print(f"      topic ZQ generated: {generated!r}")

    print("\n" + "=" * 78)
    print("PART 3 -- case C across all seven agents")
    print("   topic 'order confirmation number' is a VERBATIM PREFIX of the sentence")
    print("=" * 78)
    _, topic_c, sentence_c, _ = CASES[2]
    for agent in AGENTS:
        try:
            payload = predict(agent, [sentence_c], topic=topic_c)
            prob, kept = score_of(payload, sentence_c)
            print(f"  {agent:<30} prob={prob:<8} {'KEEP' if kept else 'DROP'}")
        except urllib.error.HTTPError as e:
            print(f"  {agent:<30} HTTP {e.code}")

    print("\n" + "=" * 78)
    print("PART 4 -- in context: identifier buried in a realistic document")
    print("=" * 78)
    doc = [
        "REMITTANCE ADVICE",
        "Invoice #: 88213-B",
        "Bill to: Northwind Trading Company, 4400 Harbor Point Road, Suite 220",
        "The total amount due is $48,215.60, payable within 30 days of the invoice date.",
        "Remittance should reference purchase order PO-77213 to ensure correct application.",
        "A late payment fee of 1.5% per month applies to balances outstanding after the due date.",
        "This document and any attachments are confidential and intended solely for the addressee.",
        "Please retain this document for your records.",
        "The audit committee reviewed the quarterly attestation package on March 5, 2026.",
        "Questions regarding this notice should be directed to the address on the reverse side.",
    ]
    topic = "invoice number, total amount due, payment terms, purchase order"
    payload = predict("general_context_agent", doc, topic=topic)
    print(f"  topic: {topic}")
    print(f"  kept {payload['relevant_count']}/{payload['total_sentences']}\n")
    for s in payload.get("scores", []):
        print(f"    {s['relevant_prob']:>8}  {'KEEP' if s['relevant'] else 'DROP'}  {s['sentence'][:62]}")
    print("\n  -> 'Invoice #: 88213-B' is dropped, so a downstream extractor asked for")
    print("     the invoice number returns null with no error raised.")

    print("\n" + "=" * 78)
    print(
        f"SUMMARY: {len(wrong)} of {len([c for c in CASES if c[3] == 'relevant'])} "
        f"relevant sentences were dropped in Part 1"
    )
    for cid, topic, sentence, prob in wrong:
        print(f"  case {cid}: prob={prob} topic={topic!r} sentence={sentence!r}")
    print("=" * 78)


if __name__ == "__main__":
    main()
