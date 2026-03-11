import argparse
import json
from pathlib import Path


def _print_rows(rows):
    print(json.dumps(rows, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(prog="smileclaw")
    parser.add_argument("--config", default="docs/gateway.yaml")

    sub = parser.add_subparsers(dest="command")

    pairing = sub.add_parser("pairing")
    pairing_sub = pairing.add_subparsers(dest="pairing_command")
    pairing_sub.add_parser("list")
    approve_pairing = pairing_sub.add_parser("approve")
    approve_pairing.add_argument("id")
    reject_pairing = pairing_sub.add_parser("reject")
    reject_pairing.add_argument("id")
    revoke_pairing = pairing_sub.add_parser("revoke")
    revoke_pairing.add_argument("principal")

    approval = sub.add_parser("approval")
    approval_sub = approval.add_subparsers(dest="approval_command")
    approval_sub.add_parser("list")
    approve_approval = approval_sub.add_parser("approve")
    approve_approval.add_argument("id")
    reject_approval = approval_sub.add_parser("reject")
    reject_approval.add_argument("id")

    runs = sub.add_parser("runs")
    runs_sub = runs.add_subparsers(dest="runs_command")
    runs_sub.add_parser("list")
    run_get = runs_sub.add_parser("get")
    run_get.add_argument("run_id")

    gateway = sub.add_parser("gateway")
    gateway_sub = gateway.add_subparsers(dest="gateway_command")
    gateway_sub.add_parser("start")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return
    if args.command == "pairing" and not args.pairing_command:
        pairing.print_help()
        return
    if args.command == "approval" and not args.approval_command:
        approval.print_help()
        return
    if args.command == "runs" and not args.runs_command:
        runs.print_help()
        return
    if args.command == "gateway" and not args.gateway_command:
        gateway.print_help()
        return

    from gateway.app import build_service
    config_path = Path(args.config)
    service = build_service(config_path)

    if args.command == "pairing":
        if args.pairing_command == "list":
            _print_rows(service.pairing.list_requests())
            return
        if args.pairing_command == "approve":
            print(json.dumps(service.pairing.approve(args.id), ensure_ascii=False, indent=2))
            return
        if args.pairing_command == "reject":
            print(json.dumps(service.pairing.reject(args.id), ensure_ascii=False, indent=2))
            return
        if args.pairing_command == "revoke":
            service.pairing.revoke(args.principal)
            print("revoked")
            return

    if args.command == "approval":
        if args.approval_command == "list":
            _print_rows(service.list_pending_approvals())
            return
        if args.approval_command == "approve":
            resp = service.resolve_approval(args.id, "approve")
            print(json.dumps(resp.__dict__, ensure_ascii=False, indent=2))
            return
        if args.approval_command == "reject":
            resp = service.resolve_approval(args.id, "reject")
            print(json.dumps(resp.__dict__, ensure_ascii=False, indent=2))
            return

    if args.command == "runs":
        if args.runs_command == "list":
            _print_rows(service.list_runs())
            return
        if args.runs_command == "get":
            print(json.dumps(service.get_run(args.run_id), ensure_ascii=False, indent=2))
            return

    if args.command == "gateway":
        if args.gateway_command == "start":
            from gateway.app import run_gateway
            run_gateway(config_path)
            return


if __name__ == "__main__":
    main()
