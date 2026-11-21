# Recorded Ember chat-history payloads

Each file is one unedited `GET chat-history?include_live_narrative=true&message_limit=2` response from a local
Skyvern stack (`ENV=local`, app URL `http://localhost:8080`; `created_trigger_webhook.json` came from a stack on
`http://localhost:5247`). `/v1/ember/chat-history` and `/v1/workflow/copilot/chat-history` are the same handler.
Times are UTC on 2026-11-14.

| File | Chat | Turn | Recorded | How the state was produced |
|---|---|---|---|---|
| `working.json` | wcc_596722129431361932 | 883b9bffcf7e427881b983b628a2ca69 | 01:07:10 | Turn running; its earlier question already answered. |
| `question.json` | wcc_596722129431361932 | 883b9bffcf7e427881b983b628a2ca69 | 01:06:42 | Turn asked "JSON or CSV?" (question card on). |
| `finished_tested_proposal.json` | wcc_596722129431361932 | 883b9bffcf7e427881b983b628a2ca69 | 01:07:52 | Turn ended with a tested proposal. |
| `question_just_resolved.json` | wcc_596709176782228474 | d64798486b7c4e11a76ace81685548c0 | 00:16:30 | Read 0.3 s after the question was answered: the narrative still says `pausedFor=question` and nothing is pending. |
| `finished_untested_proposal.json` | wcc_596800992274100812 | 5bfdf5e10c1b494ea66b44186ee02eaf | 11:13:05 | Last turn ended with disposition `review_untested`. |
| `stopped.json` | wcc_596869687476203422 | 17f238cae8394e2694f60affabb6c276 | 11:13:03 | Last turn cancelled by `skyvern_ember_stop`. |
| `waiting_on_another_person.json` | wcc_596879630152719372 | f041618bdbc94f2a87db0f0095a30b6a | 11:18:18 | Org API key started the turn and its question was pending. Roles were enforced for the org, and a member-bound key for a different user read the history, so the question and `cancel_token` are hidden. |
| `login_card.json` | wcc_596883454841800132 | c913bc19c3e24867beef6ff6af1f9340 | 11:33:00 | Studio card flags, the reader's recovery token, Redis cache on; the sign-in page is a local static form with no saved login. |
| `run_approval.json` | wcc_596877912165800232 | a705cc1f46c647c782c4d3c5a9177298 | 11:37:34 | "Run this workflow once now." on a pending draft with the single-run card on (`account_group_review`, `single_run=true`). |
| `interrupted.json` | wcc_596881528528264254 | 26f515b2cdab4ce9aa5f32b2db4a895e | 11:49:03 | Backend hard-killed mid-turn and restarted; the first read after the abandon window wrote the interrupted row. |
| `registration_card.json` | wcc_596971264822644986 | 35c19d5148454dbfaabf6ad3d217942c | 17:13:34 | Turn started through stdio `skyvern_ember_send` asking for a new account on `site_fixtures/registration_portal/` with a generated password; card is `credential_registration`, not yet attempted. |
| `site_question.json` | wcc_596972050801660396 | 560eaeef24684c5f94b46ab7bf7205e7 | 17:16:39 | Login card on a fixture origin no saved login covers, then `skyvern_ember_decide use_saved_login` with a login saved for a different site; the card carries `site_question` with `vault_sourced=false` (Skyvern-held sites). |
| `created_trigger_webhook.json` | wcc_596979390906312646 | bec7a90cc6a64ab488c69402372bb584 | 17:45:51 | Turn started through stdio `skyvern_ember_send` with a member-bound key for an org member whose role may create triggers, asking for an untested draft plus a signed webhook trigger; Ember created `etr_596979605654677482`, turned off. |

`saved_logins.json` is the `GET /v1/credentials?page=1&page_size=1000&credential_type=password` response from a local
stack on 2026-11-19 at 04:19:59 UTC, the list the Studio login card's picker shows; the route returns no secrets.
Its `created_by` user ids are set to null.

`finished_tested_proposal_saved_workflow.json` is the `GET /api/v1/workflows/wpid_596722129431361928` response for that
chat's workflow (version 1, last modified 01:06:37, before the proposal), so the judged eval can show a proposal diff.

The Gmail trigger in the trigger tests is synthesized, not recorded: Ember creates Gmail triggers only for a signed-in
Studio user with Gmail connected, and refuses every API key. Ember cannot create SFTP triggers, so no recording has one.

Cancel, resume and recovery token values in these files came from a throwaway local database and are not secrets.
The mapper test checks that none of them reaches a tool output.
