"""Offline evaluation of captured reviews against adjudicated known defects.

This scores real captured outputs; it never calls a model, runs code, or promotes
a review. Missing labels and infrastructure failures cannot become a green score.
"""

import hashlib
import json

READY = {"READY", "READY_WITH_WARNINGS"}
READINESS = READY | {"NEEDS_AUTHOR_FIX", "MANUAL_REVIEW", "INCOMPLETE"}


def export_observation(case_id, run):
    findings = []
    versions = set()
    models = set()
    failed = []
    stages = sorted(run.coordinator_stages.all(), key=lambda stage: stage.stage_id)
    preflight_stopped = any(
        stage.stage_id == "D"
        and stage.status == "D"
        and stage.output_json.get("errors")
        for stage in stages
    )
    for stage in stages:
        if stage.status == "E" or (stage.status != "D" and not preflight_stopped):
            failed.append(stage.stage_id)
        request = stage.request_json or {}
        if request.get("model"):
            models.add(request["model"])
        if request.get("prompt_version"):
            versions.add(request["prompt_version"])
        for index, finding in enumerate(stage.output_json.get("findings", [])):
            findings.append({**finding, "ref": "%s:%s" % (stage.stage_id, index)})
        if stage.stage_id == "D":
            for index, error in enumerate(stage.output_json.get("errors", [])):
                findings.append(
                    {
                        "ref": "D:preparation-%s" % index,
                        "claim": str(error),
                        "reasoning": str(error),
                        "evidence_refs": ["snapshot"],
                        "severity": "blocking",
                    }
                )
    return {
        "case_id": case_id,
        "run_id": run.pk,
        "input_hash": run.input_hash,
        "content_hash": run.content_hash,
        "models": sorted(models),
        "prompt_versions": sorted(versions),
        "readiness": run.readiness or "INCOMPLETE",
        "infrastructure_failed": bool(failed or run.status != "D"),
        "unavailable_stages": failed,
        "findings": findings,
        "adjudications": [],
        "adjudicated_by": "",
        "false_alarm_refs": [],
    }


def score_benchmark(suite, observations):
    cases = {case["id"]: case for case in suite["cases"]}
    if len(cases) != len(suite["cases"]):
        raise ValueError("Duplicate benchmark case IDs")
    reports = {}
    for observation in observations:
        identity = observation["case_id"]
        if identity not in cases or identity in reports:
            raise ValueError("Unknown or repeated observation: %s" % identity)
        if observation.get("readiness") not in READINESS:
            raise ValueError("Invalid readiness for %s" % identity)
        if not isinstance(observation.get("infrastructure_failed"), bool):
            raise ValueError("Infrastructure state must be explicit")
        if not observation.get("input_hash") or not observation.get("run_id"):
            raise ValueError("Captured run identity and input hash are required")
        reports[identity] = observation
    totals = {
        name: 0
        for name in (
            "observed",
            "evaluated",
            "missing",
            "unavailable",
            "unadjudicated",
            "false_passes",
            "false_blocks",
            "missed_defects",
            "false_alarms",
            "confirmed_detections",
            "adjudicated_defects",
        )
    }
    results = []
    for identity, case in cases.items():
        observation = reports.get(identity)
        if observation is None:
            totals["missing"] += 1
            results.append({"case_id": identity, "state": "missing", "passed": False})
            continue
        totals["observed"] += 1
        false_pass = observation["readiness"] in READY and (
            case["kind"] != "clean" or observation["infrastructure_failed"]
        )
        totals["false_passes"] += int(false_pass)
        if observation["infrastructure_failed"] and case["kind"] != "infrastructure":
            totals["unavailable"] += 1
            results.append(
                {"case_id": identity, "state": "unavailable", "passed": False}
            )
            continue
        findings = {
            finding["ref"]: finding for finding in observation.get("findings", [])
        }
        if len(findings) != len(observation.get("findings", [])):
            raise ValueError("Repeated finding references")
        expected = {defect["id"]: defect for defect in case.get("defects", [])}
        judgments = {}
        for item in observation.get("adjudications", []):
            defect = item["defect_id"]
            if (
                defect not in expected
                or defect in judgments
                or not isinstance(item.get("detected"), bool)
            ):
                raise ValueError("Invalid or repeated defect adjudication")
            if item["detected"]:
                ref = item.get("finding_ref")
                finding = findings.get(ref)
                if (
                    not finding
                    or not finding.get("evidence_refs")
                    or not finding.get("reasoning")
                ):
                    raise ValueError(
                        "A detection needs an actual evidence-backed finding"
                    )
                if ref.split(":", 1)[0] not in expected[defect]["stages"]:
                    raise ValueError("Adjudication refers to the wrong review section")
            judgments[defect] = item["detected"]
        false_alarms = observation.get("false_alarm_refs", [])
        if len(set(false_alarms)) != len(false_alarms) or any(
            ref not in findings for ref in false_alarms
        ):
            raise ValueError("False-alarm labels must reference actual findings")
        unadjudicated = len(expected.keys() - judgments.keys())
        totals["unadjudicated"] += unadjudicated
        misses = sum(not detected for detected in judgments.values())
        totals["missed_defects"] += misses
        totals["confirmed_detections"] += sum(judgments.values())
        totals["adjudicated_defects"] += len(judgments)
        totals["false_alarms"] += len(false_alarms)
        false_block = case["kind"] == "clean" and observation["readiness"] not in READY
        totals["false_blocks"] += int(false_block)
        allowed = observation["readiness"] in case["allowed_readiness"]
        adjudicator = observation.get("adjudicated_by")
        evaluated = unadjudicated == 0 and (
            case["kind"] == "infrastructure"
            or (isinstance(adjudicator, str) and bool(adjudicator.strip()))
        )
        totals["evaluated"] += int(evaluated)
        results.append(
            {
                "case_id": identity,
                "state": "evaluated" if evaluated else "unadjudicated",
                "passed": evaluated
                and allowed
                and not false_pass
                and not false_block
                and not misses
                and not false_alarms,
                "missed_defects": misses,
                "false_pass": false_pass,
                "false_block": false_block,
            }
        )
    complete = totals["evaluated"] == len(cases)
    return {
        "suite_version": suite["version"],
        "suite_hash": hashlib.sha256(
            json.dumps(suite, sort_keys=True).encode()
        ).hexdigest(),
        "expected_cases": len(cases),
        "complete": complete,
        "passed": complete and all(result["passed"] for result in results),
        "totals": totals,
        "defect_detection_rate": (
            totals["confirmed_detections"] / totals["adjudicated_defects"]
            if totals["adjudicated_defects"]
            else None
        ),
        "cases": results,
        "limitation": "A finite adjudicated benchmark is not a guarantee of publication safety or global originality.",
    }
