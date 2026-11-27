"""Server-rendered worker views. All observations come from validated exports."""

from __future__ import annotations

from datetime import datetime, timezone
from html import escape
from urllib.parse import urlencode

from endpoint_jobs import RESULTS, run_link
from endpoint_snapshot import SOURCE_NAMES

PAGES = {"/": "Overview", "/jobs": "Jobs", "/health": "Health",
         "/configuration": "Configuration", "/enrollment": "Enrollment"}
DOCS = "https://github.com/RandomDevelopment/ci-fleet/blob/main/docs/"
HEALTH_LABELS = {
    "load": "CPU load", "oom": "Out-of-memory evidence", "memory": "Memory", "swap": "Swap",
    "disk_root": "Root disk", "disk_docker": "Docker disk", "inode_root": "Root inodes",
    "inode_docker": "Docker inodes", "docker": "Docker daemon", "restarts": "Controller restarts",
    "docker_network_headroom": "Docker subnet headroom", "docker_network_inspection": "Docker network inspection",
    "capacity": "Capacity health check", "capacity_service": "Capacity sampler service",
    "timer_health": "Health timer", "timer_cleanup": "Cleanup timer", "timer_drift": "Drift timer",
    "timer_updates": "Package update timer", "timer_reconcile": "Reconciliation timer",
    "timer_capacity": "Capacity timer", "updates": "Package update status", "updates_service": "Package update service",
}
CORE_HEALTH = (
    "load", "memory", "swap", "disk_root", "disk_docker", "inode_root", "inode_docker", "oom",
    "docker", "docker_network_inspection", "docker_network_headroom", "controller", "controller_status",
    "restarts", "capacity", "capacity_service", "timer_health", "timer_cleanup", "timer_drift",
    "timer_updates", "timer_reconcile", "timer_capacity", "cleanup", "drift", "reconcile", "updates", "updates_service",
)
METRICS = {
    "host_cpu_percent": ("Host CPU", "%"), "runner_cpu_percent": ("Runner CPU", "%"),
    "host_memory_used_bytes": ("Host memory used", "bytes"),
    "runner_memory_used_bytes": ("Runner memory used", "bytes"),
    "runner_memory_limit_bytes": ("Runner memory limit", "bytes"),
    "host_swap_used_bytes": ("Host swap used", "bytes"),
    "host_disk_used_bytes_root": ("Root disk used", "bytes"),
    "host_disk_used_bytes_docker": ("Docker disk used", "bytes"),
    "host_inode_used_root": ("Root inodes used", "count"),
    "host_inode_used_docker": ("Docker inodes used", "count"),
}


def text(value):
    if value is None:
        return "Unknown"
    if type(value) is bool:
        return "Yes" if value else "No"
    if isinstance(value, list):
        return ", ".join(text(item) for item in value)
    return escape(str(value))


def words(code):
    return text(code.replace("_", " ").capitalize() if code else None)


def time_text(value):
    if value is None:
        return "Unknown time"
    try:
        stamp = datetime.fromtimestamp(value, timezone.utc)
    except (ValueError, OverflowError, OSError):
        return "Unknown time"
    return f'<time datetime="{stamp.isoformat()}">{stamp:%Y-%m-%d %H:%M:%S} UTC</time>'


def elapsed(seconds):
    if seconds is None or seconds < 0:
        return "Unknown"
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours >= 24:
        return f"{hours // 24}d {hours % 24}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def source_note(source, now):
    availability = source["availability"]
    status = words(source["freshness"] if availability == "available" else availability)
    if source["error"] and source["error"] != availability:
        status += f' · {words(source["error"])}'
    observed = source["observed_at"]
    successful = source["last_success_at"]
    when = observed if observed is not None else successful
    if when is None:
        return status + " · No measurement time"
    label = "Observed" if observed is not None else "Last successful observation"
    age = elapsed(now - when) + " ago" if now is not None and now >= when else "age unknown"
    return f'{status} · {label} {time_text(when)} · {age}'


def section(title, content, *, note=None):
    evidence = f'<p class="evidence">{note}</p>' if note else ""
    return f'<section class="panel"><h2>{title}</h2>{evidence}{content}</section>'


def definitions(rows):
    return '<dl class="definitions">' + "".join(f'<div><dt>{label}</dt><dd>{value}</dd></div>'
                                                for label, value in rows) + '</dl>'


def labeled_data(data, fields):
    return definitions((label, formatter(data.get(key))) for label, key, formatter in fields)


def chip(value):
    return f'<span class="status">{value}</span>'


def snapshot_note(snapshot):
    note = f'Publisher {words(snapshot["publisher_freshness"]).lower()} · {time_text(snapshot["published_at"])}'
    if snapshot["clock_state"] != "synchronized":
        note += f' · Host clock {words(snapshot["clock_state"]).lower()}'
    return note


def admission(snapshot):
    sources = snapshot["sources"]
    installation, controller = sources["installation"], sources["controller"]
    policy = installation["data"].get("policy_state") if installation["availability"] == "available" else None
    if policy == "drained":
        return "Intentionally drained", "The applied worker policy is drained."
    if policy == "disabled":
        return "Disabled by policy", "The applied worker policy is disabled."
    if controller["availability"] == "unavailable":
        return "Controller evidence unavailable", "No current controller measurement is available."
    if controller["freshness"] == "stale" or snapshot["publisher_freshness"] == "stale":
        return "Stale evidence", "The last observations cannot establish current admission."
    if snapshot["clock_state"] == "uncertain":
        return "Readiness unknown", "The host clock is uncertain."
    return "Readiness unknown", "Snapshot v1 carries runner counts and policy. A live admission decision is unavailable."


def overview(snapshot, now):
    sources = snapshot["sources"]
    installation, controller, configuration, health = (
        sources[name] for name in ("installation", "controller", "configuration", "health"))
    counts = controller["data"]
    policy = installation["data"]
    status, explanation = admission(snapshot)
    head = (f'<section class="admission"><h2>{status}</h2><p>{explanation}</p></section>'
            '<div class="counts">')
    shown_counts = (
        ("Busy runners", counts.get("busy"), source_note(controller, now)),
        ("Current runners", counts.get("current"), source_note(controller, now)),
        ("Configured maximum", configuration["data"].get("maximum_runners"), source_note(configuration, now)),
    )
    for label, count, note in shown_counts:
        head += f'<div class="count"><span>{label}</span><strong>{text(count)}</strong><small>{note}</small></div>'
    head += "</div>"

    restrictions = labeled_data(policy, (
        ("Installation", "record_state", words), ("Lifecycle", "lifecycle", words),
        ("Applied policy", "policy_state", words), ("Reviewed maximum", "reviewed_maximum", text),
        ("Capacity budget", "capacity_budget", text), ("Docker network policy configured", "network_policy_configured", text),
    ))
    restrictions += definitions((("Reported controller maximum", text(counts.get("maximum"))),))
    if counts.get("matches_reviewed_maximum") is False:
        restrictions += "<p>The reported controller maximum differs from the reviewed maximum.</p>"
    problems = [(HEALTH_LABELS.get(check["id"], words(check["id"])), chip(words(check["status"])))
                for check in health["data"].get("checks", []) if check["status"] != "ok"]
    if not problems:
        problems_html = ("<p>No problems in the latest health report.</p>" if health["freshness"] == "current"
                         else "<p>Current problems are unknown.</p>")
    else:
        problems_html = definitions(problems)
    if health["data"]:
        problems_html = f'<p>Reported health {chip(words(health["data"]["status"]))}</p>' + problems_html

    latest = definitions((label, source_note(sources[name], now)) for label, name in (
        ("Controller counts", "controller"), ("Health checks", "health"), ("Capacity sample", "capacity"),
        ("Listener poll", "enrollment_listener"), ("Reconciliation check", "reconciliation"), ("Host boot", "boot"),
    ))
    return (head + '<div class="columns">'
            + section("Policy and restrictions", restrictions, note=source_note(installation, now))
            + section("Reported problems", problems_html, note=source_note(health, now)) + "</div>"
            + section("Last measurements", latest))


def metric(value, unit):
    if value is None:
        return "Unknown"
    if unit == "%":
        return f"{value:.1f}%"
    if unit == "bytes":
        return f"{value / 2**20:,.1f} MiB"
    return f"{value:,}"


def health(snapshot, now):
    sources = snapshot["sources"]
    report, capacity, controller = (sources[name] for name in ("health", "capacity", "controller"))
    checks = {item["id"]: item["status"] for item in report["data"].get("checks", [])}
    rows = [(HEALTH_LABELS.get(check, words(check)), chip(words(checks.get(check)))) for check in CORE_HEALTH]
    rows += [(HEALTH_LABELS.get(check, words(check)), chip(words(value)))
             for check, value in sorted(checks.items()) if check not in CORE_HEALTH]
    report_body = ("<p>The five-minute report contains check results. Numeric subnet headroom, OOM events and "
                   "restart counts are unavailable in snapshot v1.</p>" + definitions(rows))
    paused = (snapshot["publisher_freshness"] == controller["freshness"] == "current"
              and controller["data"].get("current") == 0)
    capacity_body = ("<p>Capacity sampling runs every 30 seconds while runners are active and pauses at idle. "
                     "The seven-day percentiles use anonymous samples.</p>")
    if paused:
        capacity_body += "<p>No active runners were reported. The last sample can be older while the worker is idle.</p>"
    data = capacity["data"]
    if data:
        capacity_body += definitions((("Samples", text(data["samples"])),
                                      ("Runner observations", text(data["runner_observations"]))))
        capacity_body += ('<div class="table-wrap" tabindex="0" role="region" aria-label="Capacity percentiles">'
                          '<table><thead><tr><th scope="col">Metric</th><th scope="col">p50</th>'
                          '<th scope="col">p95</th></tr></thead><tbody>')
        for key, (label, unit) in METRICS.items():
            values = data["metrics"].get(key, {})
            capacity_body += (f'<tr><th scope="row">{label}</th><td>{metric(values.get("p50"), unit)}</td>'
                              f'<td>{metric(values.get("p95"), unit)}</td></tr>')
        capacity_body += "</tbody></table></div>"
    else:
        capacity_body += "<p>Weekly capacity measurements are unavailable.</p>"
    if paused and capacity["freshness"] == "stale":
        capacity_note = f'Last sample while active · {time_text(capacity["observed_at"])}'
    else:
        capacity_note = source_note(capacity, now)
    return ('<div class="columns">' + section("Anonymous capacity history", capacity_body, note=capacity_note)
            + section("Host checks", report_body, note=source_note(report, now)) + "</div>")


def configuration(snapshot, now):
    sources = snapshot["sources"]
    policy, reconciled, configured = (sources[name] for name in ("installation", "reconciliation", "configuration"))
    desired = sources["desired_engine_revision"]
    review = labeled_data(reconciled["data"], (
        ("Reconciliation state", "state", words), ("Last successful reconciliation", "last_success_at", time_text),
        ("Desired configuration commit", "desired_commit", text),
    ))
    review += definitions((
        ("Desired engine", text(desired["data"].get("revision"))),
        ("Desired engine configuration commit", text(desired["data"].get("config_commit"))),
        ("Desired engine evidence", source_note(desired, now)),
        ("Applied configuration commit", text(policy["data"].get("applied_commit"))),
        ("Applied engine policy", text(policy["data"].get("engine_ref"))),
        ("Applied policy evidence", source_note(policy, now)),
    ))
    review += ("<p>The desired-engine source records validation of the approved configuration. "
               "Snapshot v1 has no individual reviewer or approval record.</p>")
    runtime = []
    for label, name, field in (
        ("Selected runtime", "runtime_revision", "revision"), ("Running controller engine", "controller", "software_version"),
        ("Installed manager selection", "manager_revision", "revision"),
        ("Staged manager", "staged_manager_revision", "revision"),
    ):
        source = sources[name]
        runtime.append((label, f'{text(source["data"].get(field))}<small>{source_note(source, now)}</small>'))
    image = sources["image_verification"]
    revisions = definitions(runtime) + labeled_data(image["data"], (
        ("Image verification", "state", words), ("Verified engine revision", "revision", text),
        ("Verified architecture", "architecture", text),
    )) + f'<p class="evidence">{source_note(image, now)}</p>'
    revisions += "<p>Image verification covers local bindings recorded by the verifier. The running engine comes from controller status.</p>"
    limits = labeled_data(configured["data"], (
        ("Runner CPU cores", "runner_cpu_cores", text), ("Runner memory (MiB)", "runner_memory_mib", text),
        ("Minimum runners", "minimum_runners", text), ("Configured maximum", "maximum_runners", text),
        ("Routing labels", "routing_labels", text), ("Storage retention configured", "storage_retention_configured", text),
    ))
    limits += definitions((("Pool", text(policy["data"].get("pool"))),
                           ("Scale set", text(policy["data"].get("scale_set_name"))),
                           ("Architecture", text(policy["data"].get("architecture"))),
                           ("Policy evidence", source_note(policy, now))))
    return ('<div class="columns">' + section("Desired and applied", review, note=source_note(reconciled, now))
            + section("Engine and manager", revisions) + '</div>'
            + section("Limits and routing", limits, note=source_note(configured, now)))


def enrollment(snapshot, now):
    sources = snapshot["sources"]
    rows = []
    for label, name in (
        ("Installation record", "installation"), ("GitHub App identity", "enrollment_identity"),
        ("GitHub authentication", "enrollment_authentication"), ("Scale-set match", "enrollment_scale_set"),
        ("Listener readiness", "enrollment_listener"), ("Signed reporting", "enrollment_reporting"),
        ("GitHub App visibility", "enrollment_owner_confirmation"),
    ):
        source = sources[name]
        data = source["data"]
        state = data.get("state") or data.get("record_state")
        display = "Owner confirmation required" if name == "enrollment_owner_confirmation" else words(state or source["availability"])
        binding = f'<small>Configuration {text(data["config_commit"])}</small>' if "config_commit" in data else ""
        rows.append((label, chip(display) + f'<small>{source_note(source, now)}</small>' + binding))
    reporting = sources["installation"]["data"]
    rows.append(("Reporting required by policy", text(reporting.get("reporting_required"))
                 + f'<small>{source_note(sources["installation"], now)}</small>'))
    runbooks = (f'<p><a href="{DOCS}ADDING-A-HOST.md">Host enrollment runbook</a></p>'
                f'<p><a href="{DOCS}DESIRED-STATE.md#check-upgrade-roll-back-and-remove">Controller recovery</a></p>'
                f'<p><a href="{DOCS}HEALTH-MONITORING.md">Health checks and maintenance</a></p>')
    return ('<div class="columns">' + section("Enrollment prerequisites", definitions(rows))
            + section("Owner confirmation and recovery",
                      "<p>A GitHub App owner must confirm the App’s visibility. The snapshot has no durable "
                      "confirmation record. Credential presence, scale-set authentication and listener readiness "
                      "are separate observations.</p>" + runbooks) + "</div>")


def repository(row):
    return f'{row["owner"]}/{row["repository"]}' if "owner" in row else ""


def select_filter(label, name, choices, filters):
    options = '<option value="">All</option>'
    for value, label_text in choices:
        selected = " selected" if filters.get(name) == value else ""
        options += f'<option value="{value}"{selected}>{label_text}</option>'
    return f'<label>{label}<select name="{name}">{options}</select></label>'


def job_filters(filters):
    return ('<form class="filters" action="/jobs" method="get">'
            f'<label>Repository<input name="repository" type="search" maxlength="140" '
            f'pattern="[A-Za-z0-9_.\\/\\-]*" value="{text(filters.get("repository", ""))}"></label>'
            + select_filter("Recorded state", "state", (("running", "Running"), ("completed", "Completed"),
                             ("unknown", "Unknown")), filters)
            + select_filter("Result", "result", ((code, words(code)) for code in sorted(RESULTS)), filters)
            + '<button type="submit">Filter jobs</button></form>')


def job_row(job, now):
    name = text(job.get("display_name")) if "display_name" in job else "Job name unavailable"
    if job.get("display_truncated"):
        name += " (truncated)"
    repo = text(repository(job)) or "Repository unknown"
    url = run_link(job)
    link = f'<br><a href="{text(url)}">GitHub run {text(job["run_id"])}</a>' if url else ""
    activity = job["current_activity"]
    activity_text = "Current activity unknown" if activity == "unknown" else words(activity)
    observed = job["last_observed_at"]
    start = job.get("start_observed_at")
    finish = job.get("finished_at")
    if activity == "running" and start is not None:
        duration = f'{elapsed(now - start)}<small>Since locally observed start</small>'
    elif activity == "completed" and start is not None:
        if finish is not None and finish >= start:
            duration = f'{elapsed(finish - start)}<small>Local start to source finish</small>'
        else:
            duration = f'{elapsed(job["completion_observed_at"] - start)}<small>Local observations</small>'
    else:
        duration = "Unknown"
    gap = f'<small>{words(job["gap"])}</small>' if "gap" in job else ""
    return (f'<tr role="row"><th scope="row" role="rowheader"><bdi>{name}</bdi><small><bdi>{repo}</bdi></small>{link}</th>'
            f'<td role="cell" data-label="Activity">{chip(activity_text)}{gap}</td>'
            f'<td role="cell" data-label="Elapsed">{duration}</td>'
            f'<td role="cell" data-label="Result">{words(job.get("result"))}</td>'
            f'<td role="cell" data-label="Last observed">{time_text(observed)}'
            + (f'<small>Source finish {time_text(finish)}</small>' if finish is not None else "") + "</td></tr>")


def jobs(history, now, filters):
    source = history["activity_evidence"]
    note = f'Job export published {time_text(history["published_at"])}'
    if history["published_at"] > now:
        note += " · Clock uncertain"
    activity = "Current activity coverage verified" if history["current_activity_verified"] else "Current activity coverage unknown"
    details = [("Coverage", chip(activity)),
               ("Exported records", f'{history["exported"]:,} of {history["retained"]:,} retained'),
               ("Omitted records", f'{history["omitted"]:,} total · {history["omitted_nonterminal"]:,} nonterminal')]
    if history["covered_from"] is not None:
        details.append(("Retained observation range",
                        f'{time_text(history["covered_from"])} to {time_text(history["covered_through"])}'))
    if source:
        data = source["source"]["data"]
        details.append(("Publisher", snapshot_note(source)))
        evidence_now = None if source["clock_state"] == "uncertain" else now
        details.append(("Publisher check for job events", source_note(source["source"], evidence_now)))
        if source["generation_matches"]:
            details.append(("Last successful listener poll", time_text(data.get("last_poll_at") or None)))
            if data["pending"]:
                details.append(("Publication", "Job or coverage changes pending"))
            if not data["source_available"]:
                details.append(("Listener", "Listener source unavailable"))
            if data["gaps"] and (data["pending"] or data["gaps"] != history["gaps"]):
                gaps = ", ".join(f"{words(code)} ({count:,})" for code, count in sorted(data["gaps"].items()))
                details.append(("Publisher coverage gaps",
                                f'{gaps}<small>{source_note(source["source"], evidence_now)}</small>'))
            if data.get("demand"):
                demand = data["demand"]
                details.append(("Scale-set demand", f'{text(demand["available"])} available · '
                                f'{text(demand["assigned"])} assigned · {time_text(demand["observed_at"])}'))
        else:
            details.append(("Publication", "Publisher evidence does not match this export"))
    else:
        details.append(("Publisher", "Endpoint snapshot unavailable"))
    if history["gaps"]:
        gaps = ", ".join(f"{words(code)} ({count:,})" for code, count in sorted(history["gaps"].items()))
        details.append(("Job export coverage gaps", gaps))
    content = job_filters(filters)
    matching = history["matching"]
    if history["records"]:
        content += (f'<p class="result-count">Showing {history["offset"] + 1:,} to '
                    f'{history["offset"] + len(history["records"]):,} of {matching:,} matching records</p>'
                    '<div class="table-wrap" tabindex="0" role="region" aria-label="Job observations">'
                    '<table role="table" class="job-table"><thead role="rowgroup"><tr role="row">'
                    '<th scope="col" role="columnheader">Job and repository</th>'
                    '<th scope="col" role="columnheader">Activity</th>'
                    '<th scope="col" role="columnheader">Elapsed</th><th scope="col" role="columnheader">Result</th>'
                    '<th scope="col" role="columnheader">Last observed</th></tr></thead><tbody role="rowgroup">')
        content += "".join(job_row(row, now) for row in history["records"]) + "</tbody></table></div>"
    else:
        content += "<p>No matching retained job records.</p>"
    first = escape("/jobs" + ("?" + urlencode(filters) if filters else ""))
    next_cursor = history["next_cursor"]
    if history["offset"] or next_cursor:
        content += '<nav class="pagination" aria-label="Job pages">'
        if history["offset"]:
            content += f'<a href="{first}">First page</a>'
        if next_cursor:
            target = escape("/jobs?" + urlencode({**filters, "cursor": next_cursor}))
            content += f'<a href="{target}">Next page</a>'
        content += "</nav>"
    return section("Running and recent jobs", content) + section("Job history evidence", definitions(details), note=note)


VIEWS = {"/": overview, "/health": health, "/configuration": configuration, "/enrollment": enrollment}
NO_SOURCE = {"availability": "unknown", "freshness": "unknown", "observed_at": None,
             "last_success_at": None, "error": None, "data": {}}


def page(path, snapshot, now, filters, *, unavailable=None):
    title = PAGES[path]
    nav = "".join(f'<a href="{route}"' + (' aria-current="page"' if route == path else "") + f'>{label}</a>'
                  for route, label in PAGES.items())
    if unavailable == "cursor_expired":
        first = escape("/jobs" + ("?" + urlencode(filters) if filters else ""))
        body = section("Job page expired", f'<p>Job history changed or the record expired. <a href="{first}">Start again</a>.</p>')
    elif unavailable:
        label = "Job history unavailable" if path == "/jobs" else "Worker snapshot unavailable"
        body = section(label, f'<p>{words(unavailable)}. Measurements and current activity are unknown.</p>')
        if path == "/jobs":
            body += section("Running and recent jobs", job_filters(filters))
        else:
            body += VIEWS[path]({"sources": dict.fromkeys(SOURCE_NAMES, NO_SOURCE),
                                 "publisher_freshness": "unknown", "clock_state": "unknown"}, None)
    else:
        if path == "/jobs":
            body = jobs(snapshot, now, filters)
        else:
            body = VIEWS[path](snapshot, None if snapshot["clock_state"] == "uncertain" else now)
    if path != "/jobs" and snapshot:
        context = f'<p class="context">{text(snapshot["controller"])} · {snapshot_note(snapshot)}</p>'
    elif snapshot:
        context = f'<p class="context">{text(snapshot["controller"])} · Scale set {text(snapshot["scale_set"])}</p>'
    else:
        context = ""
    available = "false" if unavailable else "true"
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} · CI Fleet worker</title>
<link rel="stylesheet" href="/worker.css">
<script src="/worker.js" defer></script>
</head>
<body>
<a class="skip" href="#main">Skip to content</a>
<header class="site-header">
<div class="site-inner">
<p class="brand">CI Fleet <span>Worker observation</span></p>
<nav aria-label="Worker views">{nav}</nav>
</div>
</header>
<main id="main" tabindex="-1">
<h1>{title}</h1>
<p id="refresh-status" class="refresh-status" role="status"></p>
<div id="observations" data-available="{available}">
{context}
{body}
</div>
</main>
</body>
</html>
""".encode("utf-8")
