"""Тире на дефис в ответе бота (решение Ивана 22.10.2026: хук против тире в
ранах бота молчит, его строка «Исправлено» подменяла ответ)."""
import unittest

from claude_tg_bot.dashes import soften_dashes
from claude_tg_bot.replies import extract_ask_marker

EM, EN = "—", "–"


class SoftenDashesTests(unittest.TestCase):
    def test_prose_dashes_become_hyphens(self) -> None:
        self.assertEqual(soften_dashes(f"Готово {EM} проверено, 14{EN}18.10"), "Готово - проверено, 14-18.10")

    def test_text_without_dashes_unchanged(self) -> None:
        text = "Всё готово - проверено."
        self.assertIs(soften_dashes(text), text)

    def test_code_and_quotes_kept(self) -> None:
        text = (f"Команда `echo {EM}` и цитата «14{EN}20 октября», “a {EM} b”.\n"
                f"```\nx {EN} y\n```\n~~~\nz {EM} w\n~~~\nИтог {EM} так.")
        self.assertEqual(soften_dashes(text), (
            f"Команда `echo {EM}` и цитата «14{EN}20 октября», “a {EM} b”.\n"
            f"```\nx {EN} y\n```\n~~~\nz {EM} w\n~~~\nИтог - так."))

    def test_nested_quotes_blockquote_and_double_backticks_kept(self) -> None:
        text = (f"«внешняя {EM} «внутренняя {EM} цитата» {EM} конец» и {EM} после\n"
                f"> Чужая цитата {EM} дословно\n"
                f"``a {EM} b`` и {EN}")
        self.assertEqual(soften_dashes(text), (
            f"«внешняя {EM} «внутренняя {EM} цитата» {EM} конец» и - после\n"
            f"> Чужая цитата {EM} дословно\n"
            f"``a {EM} b`` и -"))

    def test_unclosed_quote_does_not_hide_rest(self) -> None:
        self.assertEqual(soften_dashes(f"кавычка « без пары {EM} тут"), "кавычка « без пары - тут")

    def test_url_does_not_eat_closing_quote(self) -> None:
        self.assertEqual(soften_dashes(f"«см. https://x.org» {EM} и «а {EM} б» {EM} в"),
                         f"«см. https://x.org» - и «а {EM} б» - в")

    def test_double_backticks_not_closed_by_longer_run(self) -> None:
        text = f"``код ``` x ` {EM} внутри`` и {EM}"
        self.assertEqual(soften_dashes(text), f"``код ``` x ` {EM} внутри`` и -")

    def test_pairs_after_unclosed_quote_kept(self) -> None:
        self.assertEqual(soften_dashes(f"«a {EM} b и «c {EM} d» {EM} e"),
                         f"«a - b и «c {EM} d» - e")

    def test_urls_kept(self) -> None:
        self.assertEqual(soften_dashes(f"См. [статья {EM} тут](https://x.org/a{EN}b) и https://y.org/c{EN}d {EM} всё"),
                         f"См. [статья - тут](https://x.org/a{EN}b) и https://y.org/c{EN}d - всё")

    def test_unclosed_fence_keeps_rest(self) -> None:
        self.assertEqual(soften_dashes(f"До {EM} блока\n```\nкод {EM} внутри"),
                         f"До - блока\n```\nкод {EM} внутри")

    def test_backticks_mid_line_are_not_a_fence(self) -> None:
        self.assertEqual(soften_dashes(f"Используй ```bash. Итог {EM} готово."),
                         "Используй ```bash. Итог - готово.")

    def test_ask_parts_softened_after_marker_extracted(self) -> None:
        text = f"Файл /tmp/a{EN}b.txt?\n<<ASK:Запускаем {EM} сейчас?|Да {EM} сразу|Нет>>"
        ask, rest = extract_ask_marker(text)
        self.assertEqual(soften_dashes(rest), "Файл /tmp/a-b.txt?")
        self.assertEqual(soften_dashes(ask[0]), "Запускаем - сейчас?")
        self.assertEqual([soften_dashes(o) for o in ask[1]], ["Да - сразу", "Нет"])


if __name__ == "__main__":
    unittest.main()
