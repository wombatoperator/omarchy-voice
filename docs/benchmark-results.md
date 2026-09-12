# Measured benchmark results

Accounted experiment spend: $1.6943 / $3.00.
Completed paid Live sessions: 71; speech fixtures: 3.

Backend completion excludes connection setup, spoken playback and idle shutdown. Desktop runs use real tools; synthetic runs fabricate tool results.

| Data | Case/input | Model/prompt | Reasoning/tier | n | Median first tool | Median completion |
| --- | --- | --- | --- | ---: | ---: | ---: |
| desktop | apps/text | gpt-5.6-luna/compact | low/default | 3 | 2.169 s | 5.303 s |
| desktop | apps/text | gpt-5.6-terra/baseline | low/default | 3 | 2.098 s | 4.148 s |
| desktop | close/text | gpt-5.6-luna/compact | low/default | 1 | 1.409 s | 7.082 s |
| desktop | close/text | gpt-5.6-terra/baseline | low/default | 2 | 2.070 s | 6.083 s |
| desktop | health/text | gpt-5.6-luna/compact | low/default | 1 | 3.265 s | 4.749 s |
| desktop | health/text | gpt-5.6-terra/baseline | low/default | 4 | 1.883 s | 3.580 s |
| desktop | health/text | gpt-5.6-terra/baseline | low/priority | 3 | 1.339 s | 2.698 s |
| desktop | news/text | gpt-5.6-luna/compact | low/default | 3 | 2.111 s | 4.473 s |
| desktop | news/text | gpt-5.6-terra/baseline | low/default | 3 | 2.615 s | 5.047 s |
| desktop | page/text | gpt-5.6-luna/compact | low/default | 1 | 1.722 s | 4.771 s |
| desktop | page/text | gpt-5.6-terra/baseline | low/default | 1 | 3.200 s | 4.579 s |
| desktop | panel/text | gpt-5.6-luna/compact | low/default | 1 | 1.374 s | 4.166 s |
| desktop | panel/text | gpt-5.6-terra/baseline | low/default | 1 | 2.192 s | 4.987 s |
| desktop | replace/text | gpt-5.6-terra/baseline | low/default | 2 | 2.806 s | 5.759 s |
| desktop | steer/speech | gpt-5.6-terra/baseline | low/default | 6 | 2.166 s | 13.875 s |
| synthetic | apps/text | gpt-5.6-luna/compact | low/default | 1 | 2.265 s | 3.416 s |
| synthetic | apps/text | gpt-5.6-terra/baseline | low/default | 1 | 2.621 s | 3.711 s |
| synthetic | apps/text | gpt-5.6-terra/compact | low/default | 1 | 2.841 s | 4.094 s |
| synthetic | close/text | gpt-5.6-luna/compact | low/default | 1 | 1.315 s | 3.627 s |
| synthetic | close/text | gpt-5.6-terra/baseline | low/default | 1 | 2.395 s | 5.775 s |
| synthetic | close/text | gpt-5.6-terra/compact | low/default | 1 | 1.315 s | 3.728 s |
| synthetic | health/speech | gpt-5.6-luna/compact | low/default | 1 | 1.728 s | 3.234 s |
| synthetic | health/text | gpt-5.6-luna/compact | low/default | 3 | 1.684 s | 2.946 s |
| synthetic | health/text | gpt-5.6-terra/baseline | low/default | 3 | 1.724 s | 3.491 s |
| synthetic | health/text | gpt-5.6-terra/baseline | low/priority | 3 | 1.563 s | 2.763 s |
| synthetic | health/text | gpt-5.6-terra/baseline | none/default | 3 | 1.668 s | 3.697 s |
| synthetic | health/text | gpt-5.6-terra/compact | low/default | 3 | 1.683 s | 3.763 s |
| synthetic | news/text | gpt-5.6-luna/compact | low/default | 1 | 2.246 s | 3.745 s |
| synthetic | news/text | gpt-5.6-terra/baseline | low/default | 1 | 3.043 s | 5.131 s |
| synthetic | news/text | gpt-5.6-terra/compact | low/default | 1 | 2.286 s | 3.522 s |
| synthetic | page/text | gpt-5.6-luna/compact | low/default | 1 | 1.411 s | 3.542 s |
| synthetic | page/text | gpt-5.6-terra/baseline | low/default | 1 | 1.467 s | 6.190 s |
| synthetic | page/text | gpt-5.6-terra/compact | low/default | 1 | 1.232 s | 4.948 s |
| synthetic | panel/text | gpt-5.6-luna/compact | low/default | 1 | 1.538 s | 3.535 s |
| synthetic | panel/text | gpt-5.6-terra/baseline | low/default | 1 | 2.300 s | 3.877 s |
| synthetic | panel/text | gpt-5.6-terra/compact | low/default | 1 | 1.417 s | 3.538 s |
| synthetic | steer/speech | gpt-5.6-luna/compact | low/default | 4 | 1.490 s | 11.301 s |

Failures and uncertain outcomes require inspecting the owner-only detail logs. A model finishing its response does not establish task success. Early app/news checks were revised to wait for settled browser identities; do not treat their original Boolean scores as comparable to reruns.
