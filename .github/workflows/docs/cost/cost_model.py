#!/usr/bin/env python3
"""Cost model for report Section 6 (commit 17). Writes docs/report/sec-6-cost.md.

    python docs/cost/cost_model.py

Every price and every assumption is in the tables below, with where it came from.
Change a number, run it again, and the section is rewritten with the new totals.
"""
import json
import math
import pathlib
from datetime import date

ROOT = pathlib.Path(__file__).resolve().parents[2]
OUT = ROOT / "docs" / "report" / "sec-6-cost.md"

INR_PER_USD = 88.0          # assumption: rate used for every conversion below
HOURS_PER_MONTH = 730

# --- What CoWIN saw on 28 April 2021 (sources S1-S4) -------------------------------------------
REG_DAY = 1.33e7            # registrations on the day (S1)
REG_PEAK_3H = 8.0e6         # registrations 4-7 pm (S2)
PEAK_HITS = 55_000          # hits per second at peak (S2, S3)
SMS_DAY = 2.78e7            # SMS delivered on the day (S4)

PEAK_RATE = REG_PEAK_3H / (3 * 3600)              # = 741 registrations/s
OFF_PEAK_RATE = (REG_DAY - REG_PEAK_3H) / (21 * 3600)
HITS_PER_REG = PEAK_HITS / PEAK_RATE              # hits/s per registration/s
SMS_PER_REG = SMS_DAY / REG_DAY

# --- Prices, USD, Mumbai (ap-south-1) unless noted ---------------------------------------------
PRICE = {
    "ec2_c7i_large": (0.0892, "c7i.large on-demand per hour", "S5"),
    "ec2_c7i_large_1y": (0.059, "c7i.large 1-year, no upfront, per hour", "S6"),
    "rds_r6g_large_maz": (0.513, "RDS PostgreSQL db.r6g.large Multi-AZ per hour", "S7"),
    "rds_1y_factor": (119 / 187, "RDS 1-year reserved / on-demand (db.r6g.large, Mumbai)", "S8"),
    "redis_r7g_large": (0.1752, "ElastiCache cache.r7g.large (Redis OSS) per hour, us-east-1 price", "S9"),
    "alb_hour": (0.0225, "ALB per hour, us-east-1 price", "S10"),
    "alb_lcu": (0.008, "ALB per LCU-hour", "S10"),
    "waf_per_million": (0.60, "WAF per million requests", "S11"),
    "waf_fixed_month": (5.0 + 10 * 1.0, "WAF: 1 web ACL ($5) + 10 rules ($1 each) per month", "S11"),
    "egress_first_10tb": (0.109, "data out to internet per GB, first 10 TB/month", "S12"),
    "egress_next_40tb": (0.085, "data out to internet per GB, next 40 TB/month", "S12"),
    "sms_aws": (0.00278, "AWS SMS to India, local transactional route, per message", "S13"),
    "sms_dlt_inr": (0.15, "Indian DLT transactional SMS, INR per message (middle of 0.10-0.18)", "S14"),
}

# --- Capacity, from our own experiments --------------------------------------------------------
MEASURED = [
    ("Lookup service, 2 x t3.micro behind the ALB", "400 requests/s, p95 98 ms, 0 failed, never broke", "commit 13, docs/evidence/13-load-lookup-*"),
    ("Registration flow on one laptop process", "40 sign-ups/s (160 requests/s), p95 8 ms, did not break", "commit 13, docs/evidence/13-load-registration-*"),
    ("PostgreSQL, 4 shards of 1 CPU each", "1,731-1,813 sign-ups/s, p95 21-24 ms: about 450 per CPU", "commit 16, docs/evidence/16-e6-*"),
    ("Autoscaling under a spike (Kubernetes)", "2 to 8 pods, first new pod 31 s after the ramp began, 0 of 71,000 failed", "commit 14, docs/evidence/14-e1-hpa-*"),
    ("A server replaced by the Auto Scaling group", "back to 2 healthy servers about 100 s after the stop", "commit 15, docs/evidence/15-replace-*"),
]

ASSUME = {
    "req_per_server": (300, "requests/s one c7i.large (2 vCPU) serves at the 60% CPU target. Two t3.micro (smaller, burstable) served 400/s without an error and we never found their limit, so 300 on a c7i.large is on the safe side"),
    "regs_per_shard": (600, "sign-ups/s per db.r6g.large shard (2 vCPU) at about 70% load; E6 measured about 450 per CPU at saturation"),
    "min_shards": (2, "shards kept even at low load, so losing one never stops everyone"),
    "min_servers": (4, "app servers kept running in Mumbai at any load, 2 per zone pair"),
    "dr_servers": (2, "warm standby app servers in Hyderabad"),
    "resp_kb": (3.0, "average response size in KB (our /centers answer is about 3 KB)"),
    "new_conn_share": (0.2, "share of requests that open a new connection (the rest reuse keep-alive)"),
    "ops_month": (300, "USD per month for CloudWatch, Route 53, backups, logs"),
    "peak_hours": (3, "busy hours per day, as on 28 April 2021 (4-7 pm)"),
    "cloud_staff": (2, "engineers to run the AWS setup (fewer than on-premises: no hardware), INR 20 lakh each per year"),
    "busy_months": (6, "months of campaign-level traffic in 3 years; the other 30 run at one tenth"),
}

# On-premises
ONPREM = {
    "server_inr": (324_806, "Dell PowerEdge R660xs starting price incl. GST (S15)"),
    "server_vcpu": (24, "vCPU-equivalents per server (Xeon Silver 4510: 12 cores, 24 threads, as in S16)"),
    "headroom": (1.25, "spare capacity per site (N+25%) so one failed server doesn't matter"),
    "sites": (2, "two data centres, like Mumbai + Hyderabad"),
    "kw_per_server": (0.35, "average draw per server, kW (assumption)"),
    "servers_per_rack": (16, "servers per rack, limited by power (assumption)"),
    "rack_inr_month": (72_500, "full rack in Mumbai per month, middle of INR 55,000-90,000 (S17)"),
    "power_inr_kw_month": (20_000, "power per kW per month, middle of INR 15,000-25,000 (S17)"),
    "network_inr_per_site": (1_500_000, "firewalls, load balancers, switches per site, one-time (assumption)"),
    "staff": (3, "engineers to run two data centres (assumption)"),
    "staff_inr_year": (2_000_000, "cost per engineer per year (assumption)"),
    "bandwidth_inr_month": (150_000, "internet transit per site per month (assumption)"),
}

SOURCES = {
    "S1": ("Scroll: nearly 1.33 crore register on day 1 (Aarogya Setu tweet)", "https://scroll.in/latest/993581/coronavirus-nearly-1-33-crore-register-for-third-phase-of-vaccinations-but-unable-to-book-slots"),
    "S2": ("DNA India: over 80 lakh registered 4-7 pm, 55,000 hits per second", "https://www.dnaindia.com/india/report-over-80-lakh-people-register-on-cowin-for-phase-3-vaccination-drive-beginning-1-may-2888267"),
    "S3": ("Free Press Journal: 383 million API hits in the first three hours", "https://www.freepressjournal.in/india/over-132-crore-people-apply-on-day-1-of-covid-19-vaccination-drive-for-18-above"),
    "S4": ("The Statesman: 1.33 crore registrations and 2.78 crore SMSes (NHA CEO)", "https://www.thestatesman.com/india/amid-technical-glitches-1-crore-enrol-covid-19-vaccination-day-1502965193.html"),
    "S5": ("DoiT: c7i.large prices in ap-south-1", "https://www.doit.com/compute/spot/ap-south-1/c7i.large"),
    "S6": ("Cloud Mercato: c7i.large billing options in Mumbai", "https://pcr.cloud-mercato.com/providers/aws/flavors/c7i.large/pricing"),
    "S7": ("Spare Cores: db.r6g.large PostgreSQL by region", "https://sparecores.com/database/aws/db.r6g.large"),
    "S8": ("Bytebase: db.r6g.large monthly on-demand and reserved prices", "https://www.bytebase.com/dbcost/rds/instance/db.r6g.large/"),
    "S9": ("Upstash: ElastiCache node prices (us-east-1)", "https://upstash.com/blog/aws-elasticache-pricing-explained-2026-full-cost-breakdown"),
    "S10": ("Costbench: ALB $0.0225 per hour (US East), $0.008 per LCU-hour", "https://www.costbench.com/software/load-balancers/aws-alb-nlb/"),
    "S11": ("AWS WAF pricing", "https://aws.amazon.com/waf/pricing/"),
    "S12": ("AWS re:Post: ap-south-1 internet egress $0.109/GB first 10 TB, $0.085 next 40 TB", "https://repost.aws/questions/QUyCjdjPdER5GbkvF-cQRWJA/can-you-please-confirm-the-exact-data-transfer-out-cost-when-exporting-data-from-amazon-s3-to-an-aws-snowball-device-in-the-mumbai-ap-south-1-region"),
    "S13": ("AWS re:Post: SNS SMS to India, local route transactional $0.00278", "https://repost.aws/questions/QUbggQc5guRoiZXuoxVjJ3aw/aws-sns-sms-pricing"),
    "S14": ("Message Central: India DLT transactional SMS INR 0.10-0.18 (2026)", "https://www.messagecentral.com/blog/a2p-sms-pricing-india"),
    "S15": ("Dell India: PowerEdge R660xs starting at INR 3,24,806 incl. GST", "https://dell.com/en-in/shop/dell-poweredge-servers/poweredge-r660xs-rack-server/spd/poweredge-r660xs/per660xs10a"),
    "S16": ("PowerEdge R660xs with Xeon Silver 4510 (configuration listing)", "https://aio.lv/en/product--dell-6wnx6--12229300"),
    "S17": ("Cyfuture: colocation prices in Mumbai (full rack, power per kW)", "https://cyfuture.cloud/kb/colocation/colocation-prices-across-major-data-centers-in-india-and-abroad"),
}


def p(key):
    return PRICE[key][0]


def a(key):
    return ASSUME[key][0]


def day_cost(peak_rate):
    """One day shaped like 28 April 2021, scaled to peak_rate registrations/s. USD."""
    scale = peak_rate / PEAK_RATE
    regs = REG_DAY * scale
    shape = [(a("peak_hours"), 1.0), (24 - a("peak_hours"), OFF_PEAK_RATE / PEAK_RATE)]
    c = {k: 0.0 for k in ("app servers", "database", "cache", "load balancers", "WAF", "data out", "SMS", "monitoring and DNS")}
    servers_peak = 0
    shards = max(a("min_shards"), math.ceil(peak_rate / a("regs_per_shard")))
    egress_gb = 0.0
    for hours, f in shape:
        hits = PEAK_HITS * scale * f
        n = max(a("min_servers"), math.ceil(hits / a("req_per_server")))
        servers_peak = max(servers_peak, n)
        c["app servers"] += hours * (n + a("dr_servers")) * p("ec2_c7i_large")
        gb_per_hour = hits * a("resp_kb") * 3600 / 1e6
        lcu = max(gb_per_hour, hits * a("new_conn_share") / 25, 1)
        c["load balancers"] += hours * (2 * p("alb_hour") + lcu * p("alb_lcu"))
        c["WAF"] += hours * hits * 3600 / 1e6 * p("waf_per_million")
        egress_gb += hours * gb_per_hour
    c["database"] = 24 * shards * p("rds_r6g_large_maz") * 1.5      # + a single-AZ replica per shard in Hyderabad
    c["cache"] = 24 * 2 * p("redis_r7g_large")
    c["WAF"] += 24 * p("waf_fixed_month") / HOURS_PER_MONTH
    c["data out"] = egress_gb * p("egress_first_10tb")
    c["SMS"] = regs * SMS_PER_REG * p("sms_aws")
    c["monitoring and DNS"] = 24 * a("ops_month") / HOURS_PER_MONTH
    return {"costs": c, "regs": regs, "servers_peak": servers_peak, "shards": shards, "egress_gb": egress_gb}


def month_cost(peak_rate):
    d = day_cost(peak_rate)
    m = {k: v * 30 for k, v in d["costs"].items()}
    gb = d["egress_gb"] * 30
    tier1 = min(gb, 10_000)
    m["data out"] = tier1 * p("egress_first_10tb") + max(gb - 10_000, 0) * p("egress_next_40tb")
    return m, d


def committed(m, d):
    """Same month with 1-year commitments on the parts that run all the time."""
    out = dict(m)
    base_servers = (a("min_servers") + a("dr_servers")) * p("ec2_c7i_large") * 24 * 30
    out["app servers"] = m["app servers"] - base_servers * (1 - p("ec2_c7i_large_1y") / p("ec2_c7i_large"))
    out["database"] = m["database"] * p("rds_1y_factor")
    out["cache"] = m["cache"] * 0.6
    return out


def onprem(peak_rate):
    d = day_cost(peak_rate)
    vcpu = d["servers_peak"] * 2 + d["shards"] * 2 * 2 * 2 + 2 * 2
    per_site = math.ceil(vcpu * ONPREM["headroom"][0] / ONPREM["server_vcpu"][0])
    servers = per_site * ONPREM["sites"][0]
    racks = ONPREM["sites"][0] * math.ceil(per_site / ONPREM["servers_per_rack"][0])
    capex = servers * ONPREM["server_inr"][0] + ONPREM["sites"][0] * ONPREM["network_inr_per_site"][0]
    month = (racks * ONPREM["rack_inr_month"][0]
             + servers * ONPREM["kw_per_server"][0] * ONPREM["power_inr_kw_month"][0]
             + ONPREM["sites"][0] * ONPREM["bandwidth_inr_month"][0]
             + ONPREM["staff"][0] * ONPREM["staff_inr_year"][0] / 12)
    return {"vcpu": vcpu, "servers": servers, "racks": racks, "capex_inr": capex, "month_inr": month}


def inr(usd):
    return usd * INR_PER_USD


def lakh(x_inr):
    return f"₹{x_inr / 1e5:,.1f} lakh"


def fmt_usd(x):
    return f"${x:,.0f}"


def load_bill():
    bills = sorted((ROOT / "docs" / "evidence").glob("17-bill-*.json"))
    if not bills:
        return None
    return json.loads(bills[-1].read_text())


def build():
    rates = [475, round(PEAK_RATE)]
    months = {r: month_cost(r) for r in rates}
    comm = {r: committed(*months[r]) for r in rates}
    on = {r: onprem(r) for r in rates}
    hi = rates[-1]

    L = ["# 6. Cost (commit 17)", "",
         f"Generated by `docs/cost/cost_model.py` on {date.today():%d %B %Y}. Every number below comes from "
         "the tables in that script; change one and run it again to update this section. Prices are "
         f"on-demand USD for Mumbai (ap-south-1) unless the table says otherwise, converted at ₹{INR_PER_USD:.0f} "
         "per dollar. Before quoting a figure outside this report, check it in the AWS Pricing Calculator "
         "(calculator.aws): AWS changes prices, and a few here are list prices from another region (marked).", "",
         "## 6.1 The load we are pricing", "",
         "On 28 April 2021, the day registration opened to everyone over 18, CoWIN took about 1.33 crore "
         "registrations [S1], 80 lakh of them between 4 and 7 pm, with a peak of 55,000 hits per second [S2], "
         "and sent 2.78 crore SMS [S4]. That gives the two rates we size for:", "",
         f"- **{PEAK_RATE:.0f} registrations/s**: the 4-7 pm average, "
         f"$\\frac{{8\\times10^{{6}}}}{{3\\times3600}}\\approx{PEAK_RATE:.0f}$.",
         "- **475 registrations/s**: the lower end of the range in Sec 9.2, a less extreme opening day.", "",
         f"From the same day: about {HITS_PER_REG:.0f} hits per second for every registration per second "
         f"($\\frac{{55000}}{{{PEAK_RATE:.0f}}}$), and {SMS_PER_REG:.1f} SMS per registration. Outside the 3 busy "
         f"hours the rest of the day averaged {OFF_PEAK_RATE:.0f} registrations/s, "
         f"{OFF_PEAK_RATE / PEAK_RATE:.0%} of the peak. Our model repeats that day shape: 3 hours at the peak, "
         "21 hours at that lower level.", "",
         "## 6.2 How many machines: from our own measurements", "",
         "| What we measured | Result | Where |", "| --- | --- | --- |"]
    L += [f"| {w} | {r} | {s} |" for w, r, s in MEASURED]
    L += ["", "From those we take these working numbers (assumptions, kept on the safe side):", "",
          "| Assumption | Value | Why |", "| --- | --- | --- |"]
    L += [f"| `{k}` | {v} | {why} |" for k, (v, why) in ASSUME.items()]
    L += ["", "| Price used | USD | Source |", "| --- | --- | --- |"]
    L += [f"| {d} | {v:g} | [{s}] |" for _, (v, d, s) in PRICE.items()]

    L += ["", "Sizing at each rate:", "",
          "| | " + " | ".join(f"{r} reg/s" for r in rates) + " |", "| --- |" + " --- |" * len(rates)]
    L.append("| Hits per second at peak | " + " | ".join(f"{PEAK_HITS * r / PEAK_RATE:,.0f}" for r in rates) + " |")
    L.append("| App servers at peak (c7i.large, Mumbai) | " + " | ".join(str(months[r][1]["servers_peak"]) for r in rates) + " |")
    L.append("| App servers off-peak | " + " | ".join(str(max(a('min_servers'), math.ceil(PEAK_HITS * r / PEAK_RATE * OFF_PEAK_RATE / PEAK_RATE / a('req_per_server')))) for r in rates) + " |")
    L.append("| Database shards (db.r6g.large Multi-AZ + Hyderabad replica) | " + " | ".join(str(months[r][1]["shards"]) for r in rates) + " |")
    L.append("| Registrations per day | " + " | ".join(f"{months[r][1]['regs'] / 1e5:,.0f} lakh" for r in rates) + " |")

    infra = {r: {k: v for k, v in months[r][0].items() if k != "SMS"} for r in rates}
    infra_c = {r: {k: v for k, v in comm[r].items() if k != "SMS"} for r in rates}
    tot_i = {r: sum(infra[r].values()) for r in rates}
    day_i = {r: sum(v for k, v in months[r][1]["costs"].items() if k != "SMS") for r in rates}
    big = max(infra[hi], key=infra[hi].get)
    L += ["", "## 6.3 The infrastructure bill", "",
          "Servers, databases, network and protection for a month in which every day peaks like 28 April 2021. "
          "That is the most a month could cost: the real campaign had a few days like that, not thirty. "
          "SMS is priced separately in 6.4 because it depends on the number of registrations, not on the "
          "machines.", "",
          "| Component | " + " | ".join(f"{r} reg/s" for r in rates) + f" | Share at {hi} |",
          "| --- |" + " --- |" * (len(rates) + 1)]
    for k in infra[hi]:
        L.append(f"| {k} | " + " | ".join(fmt_usd(infra[r][k]) for r in rates) + f" | {infra[hi][k] / tot_i[hi]:.0%} |")
    L.append("| **Per month, on-demand** | " + " | ".join(f"**{fmt_usd(tot_i[r])}** ({lakh(inr(tot_i[r]))})" for r in rates) + " | |")
    L.append("| Per month, 1-year commitments on the always-on part | " + " | ".join(f"{fmt_usd(sum(infra_c[r].values()))} ({lakh(inr(sum(infra_c[r].values())))})" for r in rates) + " | |")
    L.append("| One such day | " + " | ".join(f"{fmt_usd(day_i[r])} ({lakh(inr(day_i[r]))})" for r in rates) + " | |")
    L += ["", f"The largest item is {big} ({infra[hi][big] / tot_i[hi]:.0%}). "
          + ("WAF charges per request inspected, so at 55,000 hits a second it costs more than the servers "
             "behind it. Inspecting only the API calls, and serving the page itself from a CDN, would cut it. "
             if big == "WAF" else "")
          + "App servers are a small share because the Auto Scaling group runs the peak fleet for 3 hours "
          "a day and a much smaller one the other 21. Commitments barely help for the same reason: most "
          "of the bill is the elastic part.", ""]

    sms_reg = SMS_PER_REG * p("sms_aws")
    sms_reg_dlt_inr = SMS_PER_REG * p("sms_dlt_inr")
    infra_reg = day_i[hi] / months[hi][1]["regs"]
    share = sms_reg / (sms_reg + infra_reg)
    per_crore = 1e8
    L += ["## 6.4 Cost per registration: SMS is most of it", "",
          "| Per registration, at " + str(hi) + " reg/s | USD | INR |", "| --- | --- | --- |",
          f"| Infrastructure (one peak day / registrations that day) | {infra_reg:.6f} | {inr(infra_reg):.4f} |",
          f"| SMS, {SMS_PER_REG:.1f} messages via AWS local route [S13] | {sms_reg:.5f} | {inr(sms_reg):.3f} |",
          f"| SMS, {SMS_PER_REG:.1f} messages via an Indian DLT provider [S14] | {sms_reg_dlt_inr / INR_PER_USD:.5f} | {sms_reg_dlt_inr:.3f} |", "",
          f"**SMS is {share:.0%} of the cost of a registration.** For every 10 crore registrations: SMS "
          f"₹{inr(sms_reg) * per_crore / 1e7:,.1f} crore on AWS's route or ₹{sms_reg_dlt_inr * per_crore / 1e7:,.1f} "
          f"crore through a DLT provider, against about ₹{inr(infra_reg) * per_crore / 1e5:,.1f} lakh of "
          "infrastructure.", "",
          "Two consequences:", "",
          f"- The per-mobile OTP limits from commit 12 save money, not just capacity. Without them, E4's "
          f"single bot machine caused 1,200 SMS a minute; at ${p('sms_aws')} each that is "
          f"${1200 * 60 * p('sms_aws'):.0f} an hour from one machine, and a botnet multiplies it.",
          "- The SMS route matters more than any instance choice. AWS's default route to India is the "
          "international one, which costs about 25 times more [S13]; the DLT registration (sender ID and "
          "message templates) has to be done before launch.", "",
          "## 6.5 Cloud or our own data centres?", "",
          "On-premises has to be built for the peak all the time, in two places. The same peak, with "
          "N+25% spare per site and a second site for disaster recovery:", "",
          "| | " + " | ".join(f"{r} reg/s" for r in rates) + " |", "| --- |" + " --- |" * len(rates)]
    L.append("| vCPUs needed at peak (app + databases with standbys + cache) | " + " | ".join(str(on[r]["vcpu"]) for r in rates) + " |")
    L.append("| Servers (both sites) | " + " | ".join(str(on[r]["servers"]) for r in rates) + " |")
    L.append("| Racks | " + " | ".join(str(on[r]["racks"]) for r in rates) + " |")
    L.append("| Up front (servers, network) | " + " | ".join(lakh(on[r]["capex_inr"]) for r in rates) + " |")
    L.append("| Per month (racks, power, bandwidth, 3 engineers) | " + " | ".join(lakh(on[r]["month_inr"]) for r in rates) + " |")

    L += ["", "Three years, infrastructure and the people to run it (SMS costs the same either way):", "",
          "| | " + " | ".join(f"{r} reg/s" for r in rates) + " |", "| --- |" + " --- |" * len(rates)]
    cloud3, own3 = {}, {}
    for r in rates:
        busy = sum(infra_c[r].values())
        qm, qd = month_cost(r / 10)
        quiet = sum(v for k, v in committed(qm, qd).items() if k != "SMS")
        staff = a("cloud_staff") * ONPREM["staff_inr_year"][0] * 3 / INR_PER_USD
        cloud3[r] = (busy * a("busy_months") + quiet * (36 - a("busy_months")) + staff, busy * 36 + staff)
        own3[r] = on[r]["capex_inr"] + 36 * on[r]["month_inr"]
    L.append(f"| AWS + {a('cloud_staff')} engineers, campaign shape ({a('busy_months')} peak months, then a tenth of the load) | " + " | ".join(lakh(inr(cloud3[r][0])) for r in rates) + " |")
    L.append(f"| AWS + {a('cloud_staff')} engineers, peak every day for 3 years | " + " | ".join(lakh(inr(cloud3[r][1])) for r in rates) + " |")
    L.append(f"| Own data centres + {ONPREM['staff'][0]} engineers, 3 years | " + " | ".join(lakh(own3[r]) for r in rates) + " |")
    camp_ratio = inr(cloud3[hi][0]) / own3[hi]
    busy_ratio = inr(cloud3[hi][1]) / own3[hi]
    L += ["", f"For a campaign shaped like CoWIN's, busy for a few months and quieter afterwards, AWS costs "
          f"about {camp_ratio:.0%} of owning the hardware at {hi} reg/s, with nothing to buy before the first "
          "registration and no hardware left over afterwards. "
          + (f"If the busiest day really repeated every day for three years, owning would be cheaper "
             f"(AWS at {busy_ratio:.0%} of it): steady, predictable load at full size is where your own "
             "hardware pays off. "
             if busy_ratio > 1 else
             f"Even with the peak every day for three years AWS stays below owning ({busy_ratio:.0%}). ")
          + "The on-premises figures are on the low side: they use Dell's starting price, assumed network "
          f"and staff costs, and no software licences, spares or hardware refresh, and buying and installing "
          f"{on[hi]['servers']} servers in two cities takes months, not minutes.", ""]

    L += ["## 6.6 What our own AWS work cost", ""]
    bill = load_bill()
    if bill:
        total = sum(x["usd"] for x in bill["services"])
        L += [f"From AWS Cost Explorer, {bill['start']} to {bill['end']} "
              f"(`docs/evidence/{bill['file']}`):", "",
              "| Service | USD | INR |", "| --- | --- | --- |"]
        for x in sorted(bill["services"], key=lambda s: -s["usd"]):
            if x["usd"] >= 0.005:
                L.append(f"| {x['service']} | {x['usd']:.2f} | {inr(x['usd']):.0f} |")
        L += [f"| **Total** | **{total:.2f}** | **{inr(total):.0f}** |", "",
              f"Every AWS experiment in this report (commits 4 to 15) together cost ₹{inr(total):,.0f}, because "
              "everything was built from scripts, used for an hour or two, and deleted. The national design "
              "works the same way: pay for the peak while it lasts, then let it shrink.", ""]
    else:
        L += ["Not filled yet: run `./docs/cost/phase1_bill.sh`, then this script again.", ""]

    L += ["## 6.7 What this estimate leaves out", "",
          "- Static files (the web page, images) would be served from CloudFront and S3; small next to the rest.",
          "- Taxes: AWS bills in India add 18% GST.",
          "- People: the cloud version still needs engineers, but no one to replace disks or drive to a data centre.",
          "- Prices change. The ALB, ElastiCache and WAF prices are from other regions' list prices and are "
          "close to, but not exactly, Mumbai's.", "",
          "## Sources", ""]
    L += [f"- [{k}] [{t}]({u})" for k, (t, u) in SOURCES.items()]
    L.append("")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(L))
    print(f"{hi} reg/s: infrastructure {fmt_usd(tot_i[hi])} per peak month ({lakh(inr(tot_i[hi]))}); "
          f"per registration ${infra_reg:.6f} infrastructure + ${sms_reg:.5f} SMS (SMS {share:.0%})")
    print(f"Wrote {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    build()
