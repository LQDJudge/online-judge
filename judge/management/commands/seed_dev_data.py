"""
python3 manage.py seed_dev_data [--clear] [--seed N]

PCT-based seeder: all data generated procedurally from config tables.

Creates:
  - 1 admin superuser  (admin / devpass123)
  - 60 regular users   (password: devpass123)
  - 5 organizations
  - 210 problems across 10 categories
  - 14 blog posts
  - 27 contests  (past rated, past unrated, live, upcoming)
  - ~4 000–6 000 submissions with realistic per-tier distributions
"""

import random
from datetime import timedelta

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.db.models import Max
from django.utils import timezone

# ── Config tables ─────────────────────────────────────────────────────────────

_LANGUAGE_DEFS = [
    dict(
        key="CPP17",
        name="C++17",
        short_name="C++17",
        common_name="C++",
        ace="c_cpp",
        pygments="cpp",
        extension="cpp",
    ),
    dict(
        key="CPP14",
        name="C++14",
        short_name="C++14",
        common_name="C++",
        ace="c_cpp",
        pygments="cpp",
        extension="cpp",
    ),
    dict(
        key="PY3",
        name="Python 3",
        short_name="PY3",
        common_name="Python",
        ace="python",
        pygments="python3",
        extension="py",
    ),
    dict(
        key="JAVA",
        name="Java",
        short_name="Java",
        common_name="Java",
        ace="java",
        pygments="java",
        extension="java",
    ),
    dict(
        key="PAS",
        name="Pascal",
        short_name="Pascal",
        common_name="Pascal",
        ace="pascal",
        pygments="delphi",
        extension="pas",
    ),
]

# (tier, count, rating_lo, rating_hi, lang_key, max_difficulty, ac_base_prob)
_SKILL_TIERS = [
    ("grandmaster", 5, 2200, 2800, "CPP17", 100, 0.88),
    ("master", 8, 1900, 2199, "CPP17", 85, 0.72),
    ("expert", 12, 1600, 1899, "CPP17", 70, 0.58),
    ("specialist", 15, 1200, 1599, "CPP14", 52, 0.38),
    ("pupil", 10, 900, 1199, "PY3", 36, 0.22),
    ("newbie", 10, 100, 899, "PY3", 22, 0.10),
]

# slug, name, short_name, is_open
_ORG_DEFS = [
    ("lqdoj-staff", "LQDOJ Staff", "Staff", False),
    ("algo-club", "Algorithm Club", "AlgoClub", True),
    ("icpc-vn", "ICPC Vietnam", "ICPC", False),
    ("beginners", "Beginners Circle", "Beginners", True),
    ("adv-setters", "Advanced Problem Setters", "APS", False),
]

# prefix, display_name, group_idx(0=beg 1=int 2=adv), diff_min, diff_max, pts_min, pts_max, time_limit, count
_PROB_CATS = [
    ("impl", "Implementation", 0, 5, 28, 1, 12, 1.0, 20),
    ("math", "Mathematics", 0, 5, 38, 1, 18, 1.0, 20),
    ("sort", "Sorting", 0, 5, 24, 1, 8, 1.0, 20),
    ("bs", "Binary Search", 1, 14, 52, 8, 22, 1.0, 20),
    ("greedy", "Greedy", 1, 18, 62, 12, 28, 2.0, 20),
    ("dp", "Dynamic Prog.", 1, 24, 72, 18, 38, 2.0, 25),
    ("graph", "Graph Theory", 2, 28, 82, 22, 48, 2.0, 25),
    ("str", "Strings", 2, 24, 72, 18, 38, 2.0, 20),
    ("ds", "Data Structures", 2, 32, 88, 28, 58, 2.0, 20),
    ("adv", "Advanced", 2, 48, 99, 38, 99, 3.0, 20),
]
# Total: 20*8 + 25*2 = 210 problems

_BLOG_TITLES = [
    ("Welcome to LQDOJ Dev Environment", True, 30),
    ("Announcement: System Maintenance", False, 25),
    ("Editorial: LQDOJ Round #1", False, 20),
    ("Tips for Competitive Programming", True, 18),
    ("Editorial: LQDOJ Round #2", False, 15),
    ("New Problems Added — March 2026", False, 12),
    ("Contest Schedule — April 2026", True, 10),
    ("Editorial: LQDOJ Round #3", False, 8),
    ("How to use the judge effectively", False, 6),
    ("Results: Beginner Contest #3", False, 4),
    ("Upcoming: ICPC Warmup Series", True, 3),
    ("Problem Pack: Graph Theory", False, 2),
    ("Problem Pack: Dynamic Programming", False, 1),
    ("Leaderboard Update — April 2026", False, 0),
]

_TIMEZONES = ["Asia/Ho_Chi_Minh", "Asia/Bangkok", "Asia/Singapore", "UTC", "Asia/Tokyo"]

_SYLLABLES = [
    "minh",
    "anh",
    "tuan",
    "linh",
    "huy",
    "nam",
    "khoa",
    "long",
    "dung",
    "trang",
    "hoa",
    "duc",
    "bao",
    "cuong",
    "dat",
    "hai",
    "hung",
    "lan",
    "mai",
    "phuong",
    "quang",
    "son",
    "thanh",
    "thao",
    "thu",
    "tien",
    "toan",
    "trung",
    "van",
    "viet",
    "yen",
    "ky",
    "lam",
    "loc",
    "my",
    "ngoc",
    "nhat",
    "phat",
    "phuc",
    "sang",
    "tai",
    "tam",
    "thien",
    "thinh",
    "trong",
    "tung",
    "uyen",
    "vu",
    "xuan",
    "bich",
]

# ── Source snippets ───────────────────────────────────────────────────────────

_SRC = {
    "AC_CPP": "#include<bits/stdc++.h>\nusing namespace std;\nint main(){int a,b;cin>>a>>b;cout<<a+b;}",
    "AC_PY": "a,b=map(int,input().split())\nprint(a+b)",
    "WA": "#include<bits/stdc++.h>\nusing namespace std;\nint main(){cout<<42;}",
    "TLE": "int main(){while(1);}",
    "CE": "this is not valid C++",
    "MLE": "#include<bits/stdc++.h>\nusing namespace std;\nint main(){vector<int>v(1<<28);cout<<v[0];}",
    "RTE": "#include<bits/stdc++.h>\nusing namespace std;\nint main(){int*p=0;*p=1;}",
}


def _src_for(result, lang_key):
    if result == "AC":
        return _SRC["AC_PY"] if lang_key == "PY3" else _SRC["AC_CPP"]
    return _SRC.get(result, _SRC["WA"])


class Command(BaseCommand):
    help = "Seed the development database with PCT-generated sample data"

    def add_arguments(self, parser):
        parser.add_argument(
            "--clear",
            action="store_true",
            help="Delete all seeded objects before re-creating.",
        )
        parser.add_argument(
            "--seed", type=int, default=42, help="Random seed (default 42)."
        )

    def handle(self, *args, **options):
        random.seed(options["seed"])

        if options["clear"]:
            self._clear()

        self._seed_languages()
        self._seed_admin()
        user_tier_map = self._seed_users()  # profile -> tier_dict
        profiles = list(user_tier_map.keys())
        self._seed_organizations(profiles)
        prob_diff_map = self._seed_problems(profiles)  # problem -> difficulty int
        self._seed_blog_posts(profiles)
        self._seed_contests(profiles, prob_diff_map)
        self._seed_submissions(user_tier_map, prob_diff_map)
        self._update_user_points(profiles)

        self.stdout.write(self.style.SUCCESS("\n✓ Dev seed complete."))
        self.stdout.write("  admin / devpass123  (superuser)")
        self.stdout.write("  60 dev users        (password: devpass123)")
        self.stdout.write("  http://localhost:8001/accounts/login/")

    # ── Clear ─────────────────────────────────────────────────────────────────

    def _clear(self):
        from judge.models import BlogPost, Contest, Organization, Problem, Submission

        self.stdout.write(self.style.WARNING("Clearing seeded data…"))
        Submission.objects.filter(user__user__username__startswith="dev_").delete()
        Contest.objects.filter(key__startswith="dev_").delete()
        Problem.objects.filter(code__startswith="dev_").delete()
        BlogPost.objects.filter(summary="[dev-seed]").delete()
        for slug, *_ in _ORG_DEFS:
            Organization.objects.filter(slug=slug).delete()
        User.objects.filter(username__startswith="dev_").delete()
        self.stdout.write("  Done.")

    # ── Languages ─────────────────────────────────────────────────────────────

    def _seed_languages(self):
        from judge.models import Language

        self.stdout.write("\n── Languages")
        for spec in _LANGUAGE_DEFS:
            key = spec["key"]
            _, created = Language.objects.get_or_create(
                key=key, defaults={k: v for k, v in spec.items() if k != "key"}
            )
            self.stdout.write(f"  {'created' if created else 'exists '} {key}")

    # ── Admin ─────────────────────────────────────────────────────────────────

    def _seed_admin(self):
        from judge.models import Profile

        self.stdout.write("\n── Admin account")
        if not User.objects.filter(username="admin").exists():
            admin = User.objects.create_superuser(
                "admin", "admin@dev.local", "devpass123"
            )
            Profile.objects.get_or_create(user=admin)
            self.stdout.write("  created  admin (password: devpass123, superuser=True)")
        else:
            self.stdout.write("  exists   admin")

    # ── Users ─────────────────────────────────────────────────────────────────

    def _seed_users(self):
        from judge.models import Language, Profile

        self.stdout.write("\n── Users")

        lang_cache = {
            spec["key"]: Language.objects.filter(key=spec["key"]).first()
            for spec in _LANGUAGE_DEFS
        }
        used = set(User.objects.values_list("username", flat=True))
        user_tier_map = {}
        total_created = 0

        for (
            tier,
            count,
            rating_lo,
            rating_hi,
            lang_key,
            max_diff,
            ac_base,
        ) in _SKILL_TIERS:
            lang = lang_cache.get(lang_key)
            created_in_tier = 0

            for _ in range(count):
                # Generate a unique username
                for attempt in range(100):
                    syl = random.choice(_SYLLABLES)
                    num = random.randint(10, 999)
                    username = f"dev_{syl}{num}"
                    if username not in used:
                        used.add(username)
                        break
                else:
                    username = f"dev_user{len(used)}"
                    used.add(username)

                user, created = User.objects.get_or_create(
                    username=username,
                    defaults=dict(
                        first_name=syl.title(),
                        email=f"{username}@dev.local",
                        is_staff=False,
                    ),
                )
                if created:
                    user.set_password("devpass123")
                    user.save()
                    created_in_tier += 1

                profile, _ = Profile.objects.get_or_create(user=user)
                profile.timezone = random.choice(_TIMEZONES)
                profile.rating = random.randint(rating_lo, rating_hi)
                profile.points = 0.0
                if lang:
                    profile.language = lang
                profile.save()

                user_tier_map[profile] = {
                    "tier": tier,
                    "lang_key": lang_key,
                    "lang": lang,
                    "max_diff": max_diff,
                    "ac_base": ac_base,
                }

            total_created += created_in_tier
            self.stdout.write(f"  {tier:14s}  {count} slots  {created_in_tier} new")

        self.stdout.write(f"  total: {len(user_tier_map)} users ({total_created} new)")
        return user_tier_map

    # ── Organizations ─────────────────────────────────────────────────────────

    def _seed_organizations(self, profiles):
        from judge.models import Organization

        self.stdout.write("\n── Organizations")

        for idx, (slug, name, short, is_open) in enumerate(_ORG_DEFS):
            admin = profiles[idx % len(profiles)]
            org, created = Organization.objects.get_or_create(
                slug=slug,
                defaults=dict(
                    name=name,
                    short_name=short,
                    about="",
                    is_open=is_open,
                    registrant=admin,
                ),
            )
            if created:
                org.admins.set([admin])
                members = random.sample(
                    profiles, max(1, int(len(profiles) * random.uniform(0.3, 0.6)))
                )
                org.members.add(*members)
            self.stdout.write(f"  {'created' if created else 'exists '} {slug}")

    # ── Problems ──────────────────────────────────────────────────────────────

    def _seed_problems(self, profiles):
        from judge.models import Problem, ProblemGroup, ProblemType

        self.stdout.write("\n── Problem types / groups / problems")

        # Problem types (one per category display name)
        type_objs = {}
        for _, display, *_ in _PROB_CATS:
            t, _ = ProblemType.objects.get_or_create(name=display)
            type_objs[display] = t

        groups = []
        for g in ["Beginner", "Intermediate", "Advanced"]:
            obj, _ = ProblemGroup.objects.get_or_create(name=g)
            groups.append(obj)

        # Use first 8 profiles as potential authors
        author_pool = profiles[:8]
        prob_diff_map = {}
        total = 0

        for (
            prefix,
            display,
            grp_idx,
            diff_min,
            diff_max,
            pts_min,
            pts_max,
            tl,
            count,
        ) in _PROB_CATS:
            ptype = type_objs[display]
            group = groups[grp_idx]
            ml_choices = [65536, 131072, 262144]

            for i in range(1, count + 1):
                code = f"dev_{prefix}_{i:02d}"
                diff = random.randint(diff_min, diff_max)
                pts = random.randint(pts_min, pts_max)
                ml = random.choice(ml_choices)

                p, created = Problem.objects.get_or_create(
                    code=code,
                    defaults=dict(
                        name=f"{display} #{i}",
                        time_limit=float(tl),
                        memory_limit=ml,
                        points=float(pts),
                        group=group,
                        is_public=True,
                        short_circuit=False,
                        partial=False,
                        description=(
                            f"## {display} #{i}\n\n"
                            f"Solve this problem.\n\n"
                            f"**Difficulty:** {diff}/100\n\n"
                            f"### Input\nDescribed in the problem statement.\n\n"
                            f"### Output\nDescribed in the problem statement."
                        ),
                    ),
                )
                if created and author_pool:
                    p.types.set([ptype])
                    p.authors.set(
                        random.sample(
                            author_pool, random.randint(1, min(2, len(author_pool)))
                        )
                    )

                prob_diff_map[p] = diff
                total += 1

        self.stdout.write(
            f"  {total} problems  ({sum(c for *_, c in _PROB_CATS)} expected)"
        )
        return prob_diff_map

    # ── Blog posts ────────────────────────────────────────────────────────────

    def _seed_blog_posts(self, profiles):
        from judge.models import BlogPost

        self.stdout.write("\n── Blog posts")
        now = timezone.now()
        count = 0

        for title, sticky, days_ago in _BLOG_TITLES:
            author = random.choice(profiles)
            post, created = BlogPost.objects.get_or_create(
                title=title,
                defaults=dict(
                    content=f"## {title}\n\nContent for this post.",
                    summary="[dev-seed]",
                    visible=True,
                    sticky=sticky,
                    publish_on=now - timedelta(days=days_ago),
                ),
            )
            if created:
                post.authors.set([author])
                count += 1

        self.stdout.write(f"  {count} new posts  ({len(_BLOG_TITLES)} total)")

    # ── Contests ──────────────────────────────────────────────────────────────

    def _seed_contests(self, profiles, prob_diff_map):
        from judge.models import Contest, ContestProblem

        self.stdout.write("\n── Contests")

        now = timezone.now()
        problems = list(prob_diff_map.keys())

        # Split problem pool by code prefix for difficulty-aware picking
        def pool(*prefixes):
            return [
                p
                for p in problems
                if any(p.code.startswith(f"dev_{pfx}_") for pfx in prefixes)
            ]

        easy = pool("impl", "math", "sort")
        medium = pool("bs", "greedy", "dp", "str")
        hard = pool("graph", "ds", "adv")

        def pick(n_easy, n_med, n_hard):
            return (
                random.sample(easy, min(n_easy, len(easy)))
                + random.sample(medium, min(n_med, len(medium)))
                + random.sample(hard, min(n_hard, len(hard)))
            )

        specs = []

        # 10 past rated rounds (weekly, ending ~2 hours before end)
        for n in range(1, 11):
            specs.append(
                dict(
                    key=f"dev_round{n:02d}",
                    name=f"LQDOJ Round #{n}",
                    start=now - timedelta(days=n * 7 + 2, hours=14),
                    end=now - timedelta(days=n * 7 + 2, hours=10),
                    rated=True,
                    probs=pick(1, 2, 3),
                )
            )

        # 6 past unrated weekly practice sessions
        for n in range(1, 7):
            specs.append(
                dict(
                    key=f"dev_practice{n:02d}",
                    name=f"Weekly Practice #{n}",
                    start=now - timedelta(days=n * 7 + 1, hours=20),
                    end=now - timedelta(days=n * 7 + 1, hours=17),
                    rated=False,
                    probs=pick(2, 2, 1),
                )
            )

        # 5 past beginner-only contests
        for n in range(1, 6):
            specs.append(
                dict(
                    key=f"dev_beg{n:02d}",
                    name=f"Beginner Contest #{n}",
                    start=now - timedelta(days=n * 14 + 3, hours=15),
                    end=now - timedelta(days=n * 14 + 3, hours=13),
                    rated=True,
                    probs=pick(3, 1, 0),
                )
            )

        # 2 currently live
        specs.append(
            dict(
                key="dev_live01",
                name="LQDOJ Live Contest",
                start=now - timedelta(hours=1),
                end=now + timedelta(hours=3),
                rated=False,
                probs=pick(1, 2, 2),
            )
        )
        specs.append(
            dict(
                key="dev_live02",
                name="Advanced Training Session",
                start=now - timedelta(hours=2),
                end=now + timedelta(hours=2),
                rated=False,
                probs=pick(0, 2, 3),
            )
        )

        # 4 upcoming rounds
        for n in range(1, 5):
            specs.append(
                dict(
                    key=f"dev_upcoming{n:02d}",
                    name=f"LQDOJ Round #{n + 10}",
                    start=now + timedelta(days=n * 7 - 3, hours=14),
                    end=now + timedelta(days=n * 7 - 3, hours=18),
                    rated=True,
                    probs=pick(1, 2, 3),
                )
            )

        count = 0
        for s in specs:
            c, created = Contest.objects.get_or_create(
                key=s["key"],
                defaults=dict(
                    name=s["name"],
                    start_time=s["start"],
                    end_time=s["end"],
                    time_limit=None,
                    is_visible=True,
                    is_rated=s["rated"],
                    format_name="default",
                    scoreboard_visibility=Contest.SCOREBOARD_VISIBLE,
                    description=f"Welcome to **{s['name']}**!",
                ),
            )
            if created:
                c.authors.set([random.choice(profiles)])
                c.curators.set(random.sample(profiles, min(2, len(profiles))))
                for order, prob in enumerate(s["probs"], 1):
                    ContestProblem.objects.get_or_create(
                        contest=c,
                        problem=prob,
                        defaults=dict(points=prob.points, order=order),
                    )
                count += 1

        self.stdout.write(f"  {count} new contests  ({len(specs)} total)")

    # ── Submissions ───────────────────────────────────────────────────────────

    def _seed_submissions(self, user_tier_map, prob_diff_map):
        from judge.models import Language, Submission

        self.stdout.write("\n── Submissions  (this may take a moment…)")

        lang_cache = {
            spec["key"]: Language.objects.filter(key=spec["key"]).first()
            for spec in _LANGUAGE_DEFS
        }
        cpp = lang_cache.get("CPP17") or lang_cache.get("CPP14")
        now = timezone.now()
        total = 0

        for profile, tier in user_tier_map.items():
            max_diff = tier["max_diff"]
            ac_base = tier["ac_base"]
            lang_key = tier["lang_key"]
            lang = tier["lang"]

            # Problems within this user's difficulty reach
            reachable = [(p, d) for p, d in prob_diff_map.items() if d <= max_diff]
            # Attempt 50–75 % of reachable problems
            n_attempt = max(1, int(len(reachable) * random.uniform(0.50, 0.75)))
            to_attempt = random.sample(reachable, n_attempt)

            for problem, difficulty in to_attempt:
                # Skip if already seeded for this (user, problem)
                if Submission.objects.filter(user=profile, problem=problem).exists():
                    continue

                # Difficulty-adjusted AC probability
                diff_penalty = (difficulty / 100.0) * 0.40
                ac_prob = max(0.02, ac_base - diff_penalty)
                solves = random.random() < ac_prob

                if solves:
                    # 0 / 1 / 2 failed attempts before the AC
                    n_failed = random.choices([0, 1, 2], weights=[60, 30, 10])[0]
                    for _ in range(n_failed):
                        fail = random.choices(
                            ["WA", "TLE", "CE"], weights=[55, 30, 15]
                        )[0]
                        total += self._make_sub(
                            profile, problem, fail, lang, lang_key, cpp, now
                        )
                    total += self._make_sub(
                        profile, problem, "AC", lang, lang_key, cpp, now
                    )
                else:
                    # 1–3 failed attempts, no AC
                    n_fail = random.choices([1, 2, 3], weights=[50, 35, 15])[0]
                    for _ in range(n_fail):
                        fail = random.choices(
                            ["WA", "TLE", "CE", "MLE", "RTE"],
                            weights=[45, 25, 15, 10, 5],
                        )[0]
                        total += self._make_sub(
                            profile, problem, fail, lang, lang_key, cpp, now
                        )

        self.stdout.write(f"  {total} submissions created")

    def _make_sub(self, profile, problem, result, lang, lang_key, cpp_lang, now):
        from judge.models import Submission, SubmissionSource

        if result == "CE":
            exe_time, mem = None, None
            sub_lang = cpp_lang or lang
        elif result == "TLE":
            exe_time = round(problem.time_limit + random.uniform(0.1, 1.0), 3)
            mem = round(random.uniform(2048, problem.memory_limit * 0.5), 1)
            sub_lang = cpp_lang or lang
        elif result == "MLE":
            exe_time = round(random.uniform(0.1, problem.time_limit * 0.5), 3)
            mem = round(problem.memory_limit + random.uniform(512, 4096), 1)
            sub_lang = cpp_lang or lang
        else:
            exe_time = round(random.uniform(0.01, problem.time_limit * 0.85), 3)
            mem = round(random.uniform(2048, problem.memory_limit * 0.7), 1)
            sub_lang = lang if result == "AC" else (cpp_lang or lang)

        pts = float(problem.points) if result == "AC" else 0.0

        sub = Submission.objects.create(
            user=profile,
            problem=problem,
            language=sub_lang,
            status="D",
            result=result,
            time=exe_time,
            memory=mem,
            points=pts,
            case_points=pts,
            case_total=float(problem.points),
        )
        # auto_now_add=True prevents setting date in create(), use update()
        Submission.objects.filter(pk=sub.pk).update(
            date=now
            - timedelta(
                days=random.randint(0, 90),
                hours=random.randint(0, 23),
                minutes=random.randint(0, 59),
            )
        )
        SubmissionSource.objects.get_or_create(
            submission=sub,
            defaults=dict(source=_src_for(result, lang_key)),
        )
        return 1

    # ── Recompute points ──────────────────────────────────────────────────────

    def _update_user_points(self, profiles):
        self.stdout.write("\n── Recomputing user points")
        from judge.models import Submission

        for profile in profiles:
            best = (
                Submission.objects.filter(user=profile, result="AC")
                .values("problem_id")
                .annotate(best=Max("points"))
            )
            total = sum(row["best"] or 0 for row in best)
            profile.points = total
            profile.save(update_fields=["points"])

        self.stdout.write(f"  updated {len(profiles)} profiles")
