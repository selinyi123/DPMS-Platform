import unittest

from app.services.discovery import extract_bilibili_up_refs


class BilibiliCollectionExpansionTests(unittest.TestCase):
    def test_extracts_up_refs_from_collection_text(self):
        text = (
            "\u5f00\u5956\u65e5\u671f\uff1a6.26 "
            "\u9f20\u6807*1 \u2192\u3010KOORUI\u79d1\u777f\u5b98\u65b9UP\u3011\u3001\u30101521701206\u3011\n"
            "\u9a71\u868a\u624b\u73af*3 \u2192\u3010\u91d1\u58eb\u987f\u5b98\u65b9\u3011\u3001\u30101435278966\u3011\n"
            "\u5145\u7535\u611f\u8c22\u540d\u5355\uff1a\u3010\u666e\u901a\u7528\u6237\u3011\u3001\u3010563032706\u3011"
        )

        refs = extract_bilibili_up_refs(text)

        self.assertEqual(
            [
                {"name": "KOORUI\u79d1\u777f\u5b98\u65b9UP", "uid": "1521701206"},
                {"name": "\u91d1\u58eb\u987f\u5b98\u65b9", "uid": "1435278966"},
            ],
            refs,
        )

    def test_deduplicates_and_excludes_parent_source(self):
        text = (
            "\u3010\u4f60\u7684\u62bd\u5956\u5de5\u5177\u4eba\u3011\u3001\u3010100680137\u3011 "
            "\u3010UGAMING\u6e38\u4f17\u3011\u3001\u3010490670234\u3011 "
            "\u3010UGAMING\u6e38\u4f17\u3011\u3001\u3010490670234\u3011"
        )

        refs = extract_bilibili_up_refs(text, exclude_uids={"100680137"})

        self.assertEqual([{"name": "UGAMING\u6e38\u4f17", "uid": "490670234"}], refs)


if __name__ == "__main__":
    unittest.main()
