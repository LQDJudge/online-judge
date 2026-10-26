"""Worker capability requirements for immutable evidence packets."""


def required_evidence_protocol(operation, payload):
    if operation == "profile_probe":
        return 2
    if operation == "checker_probe":
        if payload.get("binary_data"):
            return 2
        if payload.get("builtin_checker") is not None and (
            payload["builtin_checker"]
            not in ("standard", "easy", "identical", "rstripped")
            or payload.get("checker_args") != {}
        ):
            return 2
        if any(
            "expected_output_b64" in case or "candidate_output_b64" in case
            for case in payload.get("cases", [])
        ):
            return 2
    return 1
