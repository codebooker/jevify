import unittest

import helpers  # noqa: F401  (puts the plugin on sys.path)
from jev import shellparse
from jev.shellparse import parse

C = "/repo"


class ShellParseTest(unittest.TestCase):
    def read(self, command):
        intent = parse(command, C)
        self.assertEqual(intent.kind, "read", command)
        return intent.path, intent.start, intent.end

    def test_reads(self):
        self.assertEqual(self.read("cat src/a.py"), ("/repo/src/a.py", None, None))
        self.assertEqual(self.read("cat -n src/a.py"), ("/repo/src/a.py", None, None))
        self.assertEqual(self.read("sed -n '10,40p' src/a.py"), ("/repo/src/a.py", 10, 40))
        self.assertEqual(self.read("sed -n 5p a.py"), ("/repo/a.py", 5, 5))
        self.assertEqual(self.read("sed -n '7,$p' a.py"), ("/repo/a.py", 7, None))
        self.assertEqual(self.read("nl -ba a.py | sed -n '1,80p'"), ("/repo/a.py", 1, 80))
        self.assertEqual(self.read("cat a.py | head -n 30"), ("/repo/a.py", 1, 30))
        self.assertEqual(self.read("head -n 20 a.py"), ("/repo/a.py", 1, 20))
        self.assertEqual(self.read("head -20 a.py"), ("/repo/a.py", 1, 20))
        self.assertEqual(self.read("head a.py"), ("/repo/a.py", 1, 10))
        self.assertEqual(self.read("tail -n 30 a.py"), ("/repo/a.py", -30, None))
        self.assertEqual(self.read("tail -n +5 a.py"), ("/repo/a.py", 5, None))
        self.assertEqual(self.read("cat /abs/b.go"), ("/abs/b.go", None, None))
        self.assertEqual(self.read("bash -lc 'cat a.py'"), ("/repo/a.py", None, None))
        self.assertEqual(self.read(["bash", "-lc", "sed -n '1,9p' a.py"]), ("/repo/a.py", 1, 9))

    def test_searches(self):
        for command in ("rg -n foo src", "grep -rn foo .", "git grep foo", "ls -la", "find . -name '*.py'",
                        "rg foo | head -n 20"):
            self.assertEqual(parse(command, C).kind, "search", command)
        self.assertNotEqual(parse("rg foo", "/a").key, parse("rg foo", "/b").key)
        self.assertEqual(parse("rg   foo", C).key, parse("rg foo", C).key)

    def test_other(self):
        for command in ("cat a.py > b.py", "cat a.py && ls", "go test ./...", "cat a.py b.py",
                        "sed -i 's/a/b/' a.py", "echo $(cat a.py)", "", "cat a.py; rm x",
                        "python3 -c 'print(1)'", "sed -n '1,5p' a.py b.py", "cat 'unterminated"):
            self.assertEqual(parse(command, C).kind, "other", command)
        self.assertEqual(parse(None, C).kind, "other")


class BatchTest(unittest.TestCase):
    def parse(self, text):
        return [(i.kind, i.path, i.start, i.end) for i in shellparse.parse_all(text, "/repo")]

    def test_reads_chained_with_and_are_each_parsed(self):
        self.assertEqual(self.parse("sed -n '1,5p' a.py && sed -n '10,20p' b.py"),
                         [("read", "/repo/a.py", 1, 5), ("read", "/repo/b.py", 10, 20)])

    def test_semicolons_and_pipelines_mix(self):
        found = self.parse("nl -ba a.py | sed -n '3,4p'; rg -n 'thing' src")
        self.assertEqual(found, [("read", "/repo/a.py", 3, 4), ("search", None, None, None)])

    def test_separators_inside_quotes_do_not_split(self):
        self.assertEqual(self.parse("rg -n 'foo;bar && baz' src"), [("search", None, None, None)])

    def test_a_real_codex_command(self):
        found = self.parse("sed -n '28,62p' index.html && sed -n '300,390p' src/main.js "
                           "&& sed -n '1,3p' src/style.css")
        self.assertEqual([i[1] for i in found],
                         ["/repo/index.html", "/repo/src/main.js", "/repo/src/style.css"])

    def test_unknown_parts_stay_other_beside_known_ones(self):
        self.assertEqual([i[0] for i in self.parse("npm test && sed -n '1,5p' a.py")], ["other", "read"])

    def test_alternation_in_a_search_pattern_is_not_a_pipe(self):
        found = self.parse("sed -n '1,115p' index.html && rg -n 'hud|overlay|pause' src/main.js")
        self.assertEqual([i[0] for i in found], ["read", "search"])
