"""Generate a synthetic input workbook; no network or AI calls."""
from pathlib import Path
from openpyxl import Workbook

out = Path("data/demo.xlsx")
out.parent.mkdir(parents=True, exist_ok=True)
wb = Workbook()
ws = wb.active
ws.title = "Книги"
ws.append(["Правообладатель", "Автор", "Название", "Тип контента", "url", "Сайт"])
ws.append(["Демо", "Анна Волкова", "Хроники Севера. Книга I", "ebook", "https://example.com/book-one", "example.com"])
ws.append(["Демо", "Волкова Анна", "Хроники Севера. Книга I", "ebook", "https://example.com/book-one#reader", "example.com"])
ws.append(["Демо", "Анна Волкова", "Хроники Севера. Книга II", "ebook", "https://example.com/book-two", "example.com"])
ws.append(["Демо", "Михаил Орлов", "Хроники Севера. Книга I", "ebook", "https://example.com/other-author", "example.com"])
wb.save(out)
print(out.resolve())
