# Oxford United Plugin – Setup Guide

## 1. Install

```bash
curl -X POST http://localhost:4420/api/plugins/install \
  -H "Content-Type: application/json" \
  -d '{"repository": "https://github.com/YOUR_USER/fiestaboard-plugin--oxford-united"}'
```

Or copy the plugin folder into `external_plugins/` / `plugins/` and restart FiestaBoard.

## 2. Enable

1. Open **http://localhost:4420**
2. Go to **Integrations**
3. Toggle **Oxford United** on
4. Optionally set a TheSportsDB API key (free key `123` is fine)
5. Adjust `max_scorers` if you want more/fewer names on the summary line
6. Save

## 3. Suggested page layouts

### Full match report (recommended)
```
{center}OXFORD UNITED
{{oxford_united.formatted_table}}
{{oxford_united.formatted_last}}
{{oxford_united.formatted_htft}}
{{oxford_united.formatted_scorers}}
Form {{oxford_united.form}}
```

### Individual scorers
```
{center}LAST RESULT
{{oxford_united.formatted_last}}
HT {{oxford_united.ht_score}}  FT {{oxford_united.ft_score}}
{{oxford_united.scorer_1}}
{{oxford_united.scorer_2}}
{{oxford_united.scorer_3}}
```

### Array access (advanced)
```
{{oxford_united.goals.0.formatted}}
{{oxford_united.goals.1.formatted}}
```

## Data notes

| Field | Source |
|-------|--------|
| FT score | Event `intHomeScore` / `intAwayScore` |
| HT score | Derived from timeline goals with minute ≤ 45 |
| Scorers | `lookuptimeline.php` – Goal / Penalty / Own Goal entries |
| Table | `lookuptable.php` for League One (id 4396) |

Timeline data is sometimes delayed or incomplete on the free tier. When no timeline is present, HT defaults to counting zero first-half goals and the scorer list is empty; FT is still shown.

## Troubleshooting

| Issue | What to try |
|-------|-------------|
| No scorers | Timeline not published yet – wait or check TheSportsDB event page |
| HT always 0-0 | No first-half goals recorded, or timeline missing |
| Rate limit | Increase `refresh_seconds` or use a free premium key |

## Force-refresh scores (dashboard)

FiestaBoard plugins cannot add a custom button to the main dashboard UI.
Use either of these instead:

### A. Integrations “Save Changes” (recommended)

1. Open **http://localhost:4420** → **Integrations**
2. Open **Oxford United**
3. Click **Save Changes** (you do not need to change any field)

Saving settings clears the plugin cache and the next board update pulls fresh
HT/FT scores, scorers, and table data.

### B. API one-liner

```bash
# Re-read plugin data (uses cache if still valid)
curl -s http://localhost:4420/api/plugins/oxford_united/data | jq .

# Force a config save cycle via API (also clears cache via on_config_change)
curl -s -X PUT http://localhost:4420/api/plugins/oxford_united/config \
  -H "Content-Type: application/json" \
  -d '{}' 
```

Then wait for the next display cycle, or trigger a board refresh from the main dashboard if your FiestaBoard version exposes one.
