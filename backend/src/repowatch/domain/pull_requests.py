from repowatch.domain.synchronization import RequiredCheck


def effective_ci_policy(configured, branch_rules):
    policy = {
        "configured": configured.get("configured", False),
        "required": list(configured.get("required", [])),
    }
    requirements = []
    for rule in branch_rules.get("rules", []):
        if rule.get("type") == "required_status_checks":
            parameters = rule.get("parameters", {})
            requirements.extend(
                (
                    item.get("context"),
                    item.get("integration_id"),
                    parameters.get("strict_required_status_checks_policy", False),
                )
                for item in parameters.get("required_status_checks", [])
            )
    legacy = branch_rules.get("legacy") or {}
    requirements.extend(
        (item.get("context"), item.get("app_id"), legacy.get("strict", False))
        for item in legacy.get("checks", [])
    )
    requirements.extend(
        (name, None, legacy.get("strict", False))
        for name in legacy.get("contexts", [])
        if name not in {item.get("context") for item in legacy.get("checks", [])}
    )
    for name, integration, strict in requirements:
        existing = next(
            (
                item
                for item in policy["required"]
                if item["name"] == name
                and (integration in {None, -1} or item["source"] == str(integration))
            ),
            None,
        )
        if existing and not strict:
            continue
        item = (
            {**existing}
            if existing
            else {
                "name": name,
                "source": str(integration) if integration not in {None, -1} else "unconfirmed",
                "kind": "check",
                "ref": "head",
            }
        )
        if strict:
            item["ref"] = "merge"
        if item not in policy["required"]:
            policy["required"].append(item)
    if requirements:
        policy["configured"] = True
    return policy


def evaluate_ci(pull, checks, statuses, policy, *, current_base_sha=None):
    if not policy.get("configured"):
        return {"state": "policy_required", "checks": []}
    evaluated = []
    for data in policy.get("required", []):
        requirement = RequiredCheck.model_validate(data)
        sha = {
            "head": pull["head"]["sha"],
            "base": current_base_sha or pull["base"]["sha"],
            "merge": pull.get("merge_commit_sha"),
        }[requirement.ref]
        candidates = []
        merge_current = requirement.ref != "merge" or {
            current_base_sha or pull["base"]["sha"],
            pull["head"]["sha"],
        }.issubset(set(pull.get("merge_parents", [])))
        for item in checks if requirement.kind == "check" else statuses:
            source = (
                str((item.get("app") or {}).get("id"))
                if requirement.kind == "check"
                else (item.get("creator") or {}).get("login")
            )
            name = item.get("name") if requirement.kind == "check" else item.get("context")
            if (
                merge_current
                and name == requirement.name
                and source == requirement.source
                and item.get("head_sha", item.get("sha")) == sha
            ):
                candidates.append(item)
        latest = max(
            candidates, key=lambda item: (item["id"], item.get("run_attempt", 1)), default=None
        )
        state = "pending"
        if latest:
            conclusion = (
                latest.get("conclusion") if requirement.kind == "check" else latest.get("state")
            )
            complete = requirement.kind == "status" or latest.get("status") == "completed"
            if complete and conclusion in requirement.accepted:
                state = "success"
            elif complete and conclusion in {
                "failure",
                "error",
                "timed_out",
            }:
                state = "failed"
        evaluated.append(
            {
                "requirement": requirement.model_dump(),
                "sha": sha,
                "state": state,
                "attempt_id": latest["id"] if latest else None,
            }
        )
    state = (
        "failed"
        if any(item["state"] == "failed" for item in evaluated)
        else ("pending" if any(item["state"] != "success" for item in evaluated) else "success")
    )
    return {"state": state, "checks": evaluated}


def active_reviews(reviews, head_sha):
    latest = {}
    for review in reviews:
        author = (review.get("user") or {}).get("login") or str(review["id"])
        # Comments and draft reviews do not replace the author's review decision.
        if review.get("state") not in {"CHANGES_REQUESTED", "APPROVED", "DISMISSED"}:
            continue
        old = latest.get(author)
        if old is None or (review.get("submitted_at") or "", review["id"]) > (
            old.get("submitted_at") or "",
            old["id"],
        ):
            latest[author] = review
    return [
        {**review, "outdated": review.get("commit_id") != head_sha}
        for review in latest.values()
        if review.get("state") == "CHANGES_REQUESTED"
    ]
