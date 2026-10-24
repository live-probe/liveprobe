---
title: Wikimedia 2020-06-11 sessionstore Kubernetes outage (conclusions)
source: wikitech-incident
kind: public_doc
family:
- oom
- cascading_5xx
source_uri: https://wikitech.wikimedia.org/wiki/Incidents/2020-06-11_sessionstore%2Bkubernetes
upstream: https://wikitech.wikimedia.org/wiki/Special:PermanentLink/2005377#L156-L185
upstream_line_map:
- 156
- 157
- 158
- 159
- 160
- 161
- 162
- 163
- 164
- 165
- 166
- 167
- 168
- 169
- 170
- 171
- 172
- 173
- 174
- 175
- 176
- 177
- 178
- 179
- 180
- 181
- 182
- 183
- 184
- 185
license: CC-BY-SA-4.0
attribution: '"2020-06-11 sessionstore+kubernetes" by Wikitech contributors, CC BY-SA
  4.0'
retrieved_at: '2026-09-26T07:00:00Z'
changes: Excerpt of conclusions and actionables; no source text changes; front matter
  adds attribution. This excerpt remains CC BY-SA 4.0.
---

== Conclusions ==
=== What went well? ===

* Outage was root caused quickly
* We were able to switchover temporarily to codfw (and back to eqiad) within 5mins each.
* It was during a timeframe many SREs were able to respond.
* Automated monitoring detected and informed of the incident quickly.
* We already had very good graphs and statistics built in our infrastructure

=== What went poorly? ===

* This could have been caught beforehand and extra capacity could have been added to the system, manually or automatically
* The dedicated nodes for sessionstore were already filled with instances and could not afford.

=== Where did we get lucky? ===

* Nowhere.

=== How many people were involved in the remediation? ===

* At least 4 SREs

== Actionables ==
* Increase Logstash ingestion capacity / handle logspam situations better https://phabricator.wikimedia.org/T255243
* Increase capacity of the sessionstore dedicated kubernetes nodes https://phabricator.wikimedia.org/T256236 {{done}}
* Increase kubernetes capacity overall https://phabricator.wikimedia.org/T252185 (codfw) and https://phabricator.wikimedia.org/T241850 (eqiad) {{done}}
* Investigate the iowait issues plaguing kubernetes nodes since 2020-05-29. https://phabricator.wikimedia.org/T255975 {{done}}
* Investigate the apparent network connectivity loss for kubernetes100[1,3,5] during the incident
* Investigate adding resource utilization alerts to services hosted on kubernetes
* Adopt SLIs/SLOs for sessionstore https://phabricator.wikimedia.org/T256629
