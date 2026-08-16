"""Command-line interface for local operation and demos.

No auth — direct local database access, same trust model as the RAG
project's CLI (see that project's app/cli.py for the rationale). Not for
network exposure.

Usage:
    python -m app.cli ingest-kb data/kb_articles
    python -m app.cli report "my VPN keeps dropping" --requester alice
    python -m app.cli report "everyone's VPN is down" --requester bob --no-deflect
    python -m app.cli queue
    python -m app.cli ack <ticket_id>
    python -m app.cli resolve <ticket_id>
    python -m app.cli check-escalations
    python -m app.cli stats
"""
import argparse
import sys

from . import alerting, db, kb
from .intake import file_ticket, start_intake

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def _print_result(r):
    if r.outcome == "deflected":
        print(f"\nSelf-service match found (score={r.kb_match.score:.2f}):")
        print(f"  {r.kb_match.title}")
        print(f"  {r.kb_match.body}\n")
        print("If this didn't resolve it, re-run with --no-deflect to file a ticket.")
    else:
        flag = " [RED-FLAG OVERRIDE]" if r.red_flag else ""
        print(f"\nTicket #{r.ticket_id} created — priority {r.priority}{flag}")
        print(f"  Reasoning: {r.reasoning}")
        if r.alert:
            print(f"  Alert: sent={r.alert['sent']} via {r.alert['channel']}")


def main():
    parser = argparse.ArgumentParser(description="IT Helpdesk Intake & Triage CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_kb = sub.add_parser("ingest-kb", help="Ingest a directory of KB articles")
    p_kb.add_argument("path")

    p_report = sub.add_parser("report", help="Report an issue")
    p_report.add_argument("description")
    p_report.add_argument("--requester", default="")
    p_report.add_argument("--no-deflect", action="store_true", help="Skip KB deflection, file a ticket directly")

    p_queue = sub.add_parser("queue", help="Show the ticket queue")
    p_queue.add_argument("--status", default=None)

    p_ack = sub.add_parser("ack", help="Acknowledge a ticket")
    p_ack.add_argument("ticket_id", type=int)

    p_resolve = sub.add_parser("resolve", help="Mark a ticket resolved")
    p_resolve.add_argument("ticket_id", type=int)

    sub.add_parser("check-escalations", help="Re-alert on unacknowledged P1s past the escalation window")
    sub.add_parser("stats", help="Show deflection/ticket stats")

    args = parser.parse_args()
    db.init_db()

    if args.command == "ingest-kb":
        ids = kb.ingest_kb_directory(args.path)
        print(f"Ingested {len(ids)} KB articles from {args.path}")

    elif args.command == "report":
        if args.no_deflect:
            r = file_ticket(args.description, requester=args.requester)
        else:
            r = start_intake(args.description, requester=args.requester)
        _print_result(r)

    elif args.command == "queue":
        with db.session() as conn:
            rows = db.list_tickets(conn, status=args.status)
        if not rows:
            print("Queue is empty.")
            return
        print(f"\n{'ID':4s} {'PRI':4s} {'STATUS':13s} {'CATEGORY':10s} {'FLAG':6s} DESCRIPTION")
        print("-" * 90)
        for r in rows:
            flag = "RED" if r["red_flag_matched"] else ""
            print(f"{r['id']:<4d} {r['priority']:4s} {r['status']:13s} {r['category']:10s} {flag:6s} {r['description'][:50]}")

    elif args.command == "ack":
        with db.session() as conn:
            db.acknowledge_ticket(conn, args.ticket_id)
            db.log_event(conn, "acknowledged", {"by": "cli"}, ticket_id=args.ticket_id)
        print(f"Ticket #{args.ticket_id} acknowledged.")

    elif args.command == "resolve":
        with db.session() as conn:
            db.resolve_ticket(conn, args.ticket_id)
            db.log_event(conn, "resolved", {"by": "cli"}, ticket_id=args.ticket_id)
        print(f"Ticket #{args.ticket_id} resolved.")

    elif args.command == "check-escalations":
        escalated = alerting.check_escalations()
        if not escalated:
            print("No unacknowledged P1s past the escalation window.")
        else:
            for e in escalated:
                print(f"Escalated ticket #{e['ticket_id']} via {e['channel']} (sent={e['sent']})")

    elif args.command == "stats":
        with db.session() as conn:
            s = db.deflection_stats(conn)
        print("\nSelf-service deflection stats:")
        for k, v in s.items():
            print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
