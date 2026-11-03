import json
import os
import re
import unittest

from _load import ROOT

# the Taiwan day-trading pack: the eleven folders (trader-institutional replaced the US Pelosi tracker)
SKILLS = ["trader-scanner", "trader-news", "trader-entry", "trader-exit", "trader-timemachine",
          "trader-risk", "trader-budget", "trader-broker", "trader-alerts", "trader-analyst",
          "trader-institutional"]
ABOUT = "11 個台股當沖 Claude skill：掃描上千檔線圖、追蹤新聞和重大訊息、抓準每一次進場和出場，並在動用真錢之前先重播每一種結果。"
REPO = "eric861129/day-trader-skill-TW"
PLUGIN = "day-trader-skill-tw"
# skills on the way to an order carry the safety block
ORDER_PATH = ["trader-scanner", "trader-entry", "trader-exit", "trader-timemachine", "trader-budget",
              "trader-institutional", "trader-risk", "trader-broker", "trader-alerts"]
# every skill runs on the Taiwan market: Chinese SKILL.md, tools on twmarket.py
TW_SKILLS = SKILLS
# what the US pack used and the Taiwan pack must not: New York time, US data sources, the Alpaca order tool
US_ONLY = ("eastern_offset", "NYSE", "New York", "sec.gov", "EDGAR", "Alpaca", "place_stock_order", "stooq")
V1_ONLY = ["trader-copycat", "trader-screener", "trader-tax", "trader-rebalance"]


def frontmatter(path):
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    return (m.group(1) if m else ""), text


def every_text_file():
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")]
        for f in files:
            if f.endswith((".md", ".py", ".json", ".csv", ".txt", ".xml")) or f == "LICENSE":
                yield os.path.join(base, f)


class Repo(unittest.TestCase):
    def test_eleven_skill_folders_at_the_root(self):
        found = sorted(d for d in os.listdir(ROOT)
                       if os.path.isfile(os.path.join(ROOT, d, "SKILL.md")))
        self.assertEqual(found, sorted(SKILLS))

    def test_root_matches_the_reel(self):
        # .git* and .claude (Claude Code's local settings and state, never committed) are not the pack
        root = sorted(d for d in os.listdir(ROOT) if not d.startswith(".git") and d != ".claude")
        self.assertEqual(root, sorted([".claude-plugin", "LICENSE", "README.md", "tests"] + SKILLS))

    def test_frontmatter_name_and_description(self):
        for s in SKILLS:
            fm, text = frontmatter(os.path.join(ROOT, s, "SKILL.md"))
            self.assertIn("name: %s\n" % s, fm + "\n", s)
            self.assertRegex(fm, r"description: ", s)
            self.assertGreater(len(fm), 200, "%s description is too thin" % s)
            self.assertGreater(len(text.splitlines()), 60, s)

    def test_order_path_skills_carry_the_safety_rule(self):
        for s in ORDER_PATH:
            _, text = frontmatter(os.path.join(ROOT, s, "SKILL.md"))
            low = " ".join(text.lower().split())
            self.assertIn("不構成投資建議", low, s)
            self.assertIn("yes", low, s)

    def test_tw_skills_run_on_the_taiwan_core(self):
        for s in TW_SKILLS:
            folder = os.path.join(ROOT, s)
            self.assertTrue(os.path.isfile(os.path.join(folder, "twmarket.py")), s)
            tools = [f for f in os.listdir(folder) if f.endswith(".py") and f != "twmarket.py"]
            for f in tools:
                with open(os.path.join(folder, f), encoding="utf-8") as fh:
                    src = fh.read()
                self.assertIn("import twmarket", src, "%s/%s" % (s, f))
                for old in US_ONLY:
                    self.assertNotIn(old, src, "%s/%s still has the US pack's %s" % (s, f, old))
            _, text = frontmatter(os.path.join(folder, "SKILL.md"))
            self.assertIn("## 安全", text, s)
            for old in US_ONLY:
                self.assertNotIn(old, text, "%s/SKILL.md still has the US pack's %s" % (s, old))

    def test_plugin_manifest_points_at_the_root_skills(self):
        with open(os.path.join(ROOT, ".claude-plugin", "plugin.json"), encoding="utf-8") as fh:
            plugin = json.load(fh)
        self.assertEqual(plugin["name"], PLUGIN)
        self.assertEqual(plugin["skills"], "./")
        self.assertEqual(plugin["description"], ABOUT)
        self.assertEqual(plugin["repository"], "https://github.com/" + REPO)
        with open(os.path.join(ROOT, ".claude-plugin", "marketplace.json"), encoding="utf-8") as fh:
            market = json.load(fh)
        self.assertEqual(market["name"], PLUGIN)
        self.assertEqual(market["plugins"][0]["name"], PLUGIN)

    def test_readme_carries_the_reel_copy(self):
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as fh:
            readme = " ".join(fh.read().split())
        self.assertTrue(readme.startswith("# 台股當沖 skill"))
        for line in (ABOUT,
                     "https://github.com/%s install skill" % REPO,
                     "/plugin marketplace add %s" % REPO,
                     "/plugin install %s@%s" % (PLUGIN, PLUGIN),
                     "## 注意事項",
                     "https://github.com/Jakeschincariol/day-trader-skill"):   # credit to the original
            self.assertIn(line, readme)
        for s in SKILLS:
            self.assertIn("/" + s, readme, s)

    def test_no_em_dashes_and_no_personal_email(self):
        for path in every_text_file():
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            rel = os.path.relpath(path, ROOT)
            if not rel.startswith(os.path.join("tests", "fixtures")):  # fixtures are real data, kept as published
                self.assertNotIn("\u2014", text, rel)
            self.assertNotIn("jakeschincariol" + "@" + "gmail.com", text, rel)  # built, so this file passes too

    def test_no_v1_only_skill_is_left_behind(self):
        for path in every_text_file():
            rel = os.path.relpath(path, ROOT)
            if rel.startswith("tests" + os.sep) or rel == "README.md":
                continue  # the README points v1 users to the v1 commit
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            for old in V1_ONLY:
                self.assertNotIn(old, text, rel)

    def test_every_tool_runs_on_python_3_8(self):
        # no 3.9+ only modules or syntax in the tools (the README promises 3.8)
        for s in SKILLS:
            folder = os.path.join(ROOT, s)
            for f in os.listdir(folder):
                if not f.endswith(".py"):
                    continue
                with open(os.path.join(folder, f), encoding="utf-8") as fh:
                    src = fh.read()
                for bad in ("import zoneinfo", "from zoneinfo", ".removeprefix(", ".removesuffix(",
                            "match ", "list[", "dict[", "tuple["):
                    if bad == "match ":
                        self.assertNotRegex(src, r"(?m)^\s+match \w+.*:$", "%s/%s" % (s, f))
                    elif bad.endswith("["):
                        self.assertNotRegex(src, r"(?<![\w.])%s" % re.escape(bad), "%s/%s" % (s, f))
                    else:
                        self.assertNotIn(bad, src, "%s/%s" % (s, f))

    def test_day_trading_limits_ship_in_the_example_config(self):
        with open(os.path.join(ROOT, "trader-budget", "config.example.json"), encoding="utf-8") as fh:
            cfg = json.load(fh)
        for k in ("max_loss_per_trade_twd", "max_daily_loss_twd", "max_trades_per_day",
                  "min_reward_risk", "require_stop", "no_entries_before", "no_entries_after", "flat_by",
                  "day_trade_enabled", "fee_discount"):
            self.assertIn(k, cfg, k)
        self.assertIs(cfg["require_stop"], True)
        self.assertEqual(cfg["flat_by"], "13:20")    # before the 13:25 closing auction
        # off until the user turns them on: 當沖 needs the broker's agreement, odd lots are held overnight
        for k in ("day_trade_enabled", "allow_overnight", "allow_disposition", "allow_short", "allow_margin"):
            self.assertIs(cfg.get(k, False), False, k)

    def test_paper_trading_is_the_default(self):
        with open(os.path.join(ROOT, "trader-budget", "config.example.json"), encoding="utf-8") as fh:
            cfg = json.load(fh)
        self.assertIs(cfg["paper"], True)
        self.assertIs(cfg.get("allow_options", False), False)


if __name__ == "__main__":
    unittest.main()
