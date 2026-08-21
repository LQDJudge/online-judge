import json
import random
import re
import time
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count, Max
from django.utils import timezone
from django.utils.html import strip_tags
from django.utils.text import slugify

from reversion import revisions

from judge.ml.semantic_search import SemanticSearchUnavailable, search_problems
from judge.models import (
    BlogPost,
    Contest,
    ContestProblem,
    Organization,
    Problem,
    Submission,
)
from llm_service.config import get_config
from llm_service.llm_api import LLMService

PROBLEM_LINK_RE = re.compile(r"/problem/([-a-z0-9_]+)")
CONTEST_LINK_RE = re.compile(r"/contest/([-a-z0-9_]+)")
TOPIC_LINE_RE = re.compile(r"^\*\*Chủ đề:\*\*\s*(.+?)\s*$", re.MULTILINE)

DIFFICULTY_RANGES = {
    "easy": (0, 900),
    "standard": (900, 1400),
    "challenge": (1400, 2000),
    "stretch": (2000, 3000),
}

DIFFICULTY_RANGES_BY_LEVEL = {
    "primary": {
        "easy": (0, 900),
    },
    "middle": {
        "easy": (0, 900),
        "standard": (850, 1250),
        "challenge": (1200, 1600),
    },
    "high": {
        "standard": (900, 1400),
        "challenge": (1300, 1900),
        "stretch": (1800, 2600),
    },
    "advanced": {
        "challenge": (1600, 2300),
        "stretch": (2200, 3200),
    },
}

DIFFICULTY_WEIGHTS = {
    "easy": 15,
    "standard": 35,
    "challenge": 35,
    "stretch": 15,
}

DIFFICULTY_WEIGHTS_BY_LEVEL = {
    "primary": {
        "easy": 100,
    },
    "middle": {
        "easy": 30,
        "standard": 50,
        "challenge": 20,
    },
    "high": {
        "standard": 30,
        "challenge": 50,
        "stretch": 20,
    },
    "advanced": {
        "challenge": 45,
        "stretch": 55,
    },
}

DIFFICULTY_QUERIES = {
    "easy": [
        "bài lập trình cơ bản vòng lặp mảng xâu đếm sắp xếp",
        "beginner programming loops arrays strings counting sorting",
    ],
    "standard": [
        "bài lập trình trung bình mảng xâu tìm kiếm tham lam prefix sum",
        "medium competitive programming arrays strings binary search greedy",
    ],
    "challenge": [
        "bài thuật toán khó quy hoạch động đồ thị cấu trúc dữ liệu số học",
        "hard competitive programming dp graph data structures number theory",
    ],
    "stretch": [
        "bài thuật toán nâng cao tối ưu hóa quy hoạch động đồ thị chia căn",
        "advanced algorithms optimization dynamic programming graph decomposition",
    ],
}

BANNED_PHRASES = (
    "trong hành trình",
    "chinh phục",
    "mổ xẻ",
    "các bạn coder",
    "hy vọng qua bài viết",
    "bước lên sân khấu",
    "thực chất",
    "tối ưu hơn",
    "hiệu quả hơn",
    "nhiệm vụ của chúng ta",
    "phản xạ tự nhiên",
    "hoàn toàn chính xác",
    "cách nghĩ đầu tiên của học sinh",
    "chiếc hộp",
    "mảng nhảy cao su",
    "nhảy cao su",
    "cô cạn",
    "độ hội tụ",
    "trong vô vọng",
    "lỗ hổng logic trong tư duy",
    "dán nguyên khối",
    "tắt tab",
    "quăng một đống",
    "đống hỗn độn",
    "đống mã",
    "tổng đoạn thẳng",
    "đống link",
    "đống file",
    "lỗi ngớ ngẩn",
    "tràn bộ nhớ",
    "đọc sót",
    "trôi đi khá nhanh",
    'cout << "WA"',
    "cứu bài",
    "mã em WA hết",
    "cuộn chuột",
    "hí hửng",
    "ngợp",
    "ngộp",
    "tư duy nào thường bị đứt gãy",
    "đứt gãy",
    "rêu phong",
    "thước phim",
    "có chiều sâu",
    "triết lý gượng gạo",
    "góc khuất",
    "ma trận công thức",
    "tự khắc",
    "hướng đi của bài toán tự",
    "trở nên rõ ràng hơn",
    "đọc đúng phần logic",
    "hiểu bản chất",
    "phép chia modulo",
    "chia lấy dư",
    "chia modulo",
    "chung chung",
    "chằm chằm",
    "đọc từ đầu đến cuối như đọc truyện",
    "bộ nhớ tạm",
    "to và tròn",
    "không gian nhẹ nhàng",
    "khởi động tay chân",
    "sa đà",
)

ABSTRACT_WORDS = (
    "quan sát",
    "cấu trúc",
    "cơ chế",
    "tính chất",
    "phạm vi",
    "ràng buộc",
    "độ phức tạp",
    "thuật toán",
    "kỹ thuật",
    "phương pháp",
    "chuẩn hóa",
    "tối ưu",
)

CONCRETE_OPENING_MARKERS = (
    "ví dụ",
    "chẳng hạn",
    "giả sử",
    "với",
    "khi",
    "nếu",
    "trong",
    "một",
    "hai",
    "ba",
    "bạn",
    "mình",
)

ENGLISH_PROSE_TERMS = (
    "workflow",
    "magazine",
    "column",
    "filler",
)

UNEXPLAINED_TECHNICAL_TERMS = {
    "dfs": ("duyệt sâu",),
    "dp": ("quy hoạch động",),
    "fenwick": ("cây chỉ số nhị phân", "cây fenwick"),
}

THCS_EXCLUDE_MARKERS = (
    "thta",
    "tht a",
    "tht bảng a",
    "tht bang a",
    "bảng a",
    "bang a",
    "tiểu học",
    "tieu hoc",
    "thtc",
    "tht c",
    "tht bảng c",
    "tht bang c",
    "bảng c",
    "bang c",
    "thpt",
    "trung học phổ thông",
    "trung hoc pho thong",
)

PRIMARY_EXCLUDE_MARKERS = (
    "hsg",
    "hsg9",
    "lớp 9",
    "lop 9",
    "ts10",
    "tuyển sinh",
    "tuyen sinh",
    "thtb",
    "tht b",
    "bảng b",
    "bang b",
    "thcs",
    "trung học cơ sở",
    "thtc",
    "tht c",
    "bảng c",
    "bang c",
    "thpt",
    "trung học phổ thông",
)

THPT_EXCLUDE_MARKERS = (
    "thta",
    "tht a",
    "bảng a",
    "bang a",
    "tiểu học",
    "tieu hoc",
    "thtb",
    "tht b",
    "bảng b",
    "bang b",
    "thcs",
    "trung học cơ sở",
)

VOI_EXCLUDE_MARKERS = (
    "hsg 9",
    "hsg9",
    "lớp 9",
    "lop 9",
    "thcs",
    "trung học cơ sở",
    "tht bảng b",
    "tht b",
    "bảng b",
)

DEFAULT_TOPIC_BANK = (
    "Từ cách làm trực tiếp đến đại lượng cần lưu",
    "Một ví dụ nhỏ có thể dẫn tới công thức như thế nào",
    "Khi nào nên gom nhiều phép tính thành một bảng phụ",
    "Đọc một bài khó bằng câu hỏi: trạng thái nào đang thay đổi?",
    "Vì sao một quyết định tham lam cần điều luôn đúng",
    "Tối ưu bằng cách không làm lại việc đã biết",
    "Cách đặt một câu hỏi dễ được cộng đồng giúp",
    "Đọc lời giải mẫu sao cho không mất ý tưởng chính",
    "Ghi chú sau khi giải bài: nên lưu lại điều gì?",
    "Trước một kỳ thi, nên đọc lại lỗi cũ như thế nào?",
    "Chia sẻ tài liệu sao cho người sau dùng được ngay",
    "Một bài đáng thử cuối tuần này",
    "Một lỗi nhỏ trong mã có thể che giấu ý tưởng đúng",
    "Cách đọc đề dài mà không bị lạc",
    "Khi nào nên dừng tối ưu và viết bản đơn giản trước?",
    "Một danh sách kiểm tra trước khi bấm nút nộp bài",
    "Cách biến một bộ kiểm thử sai thành bài học ngắn",
    "Một tài nguyên học thuật toán nên đọc chậm",
    "Một cuộc thảo luận hay bắt đầu từ ví dụ nào?",
    "Tuần này nên luyện một dạng bài hay một kỹ thuật?",
    "Nhìn lại một kỳ thi: nên học gì sau bảng điểm?",
    "Một câu chuyện nhỏ sau giờ học",
    "Cách chia sẻ kinh nghiệm thi mà không biến thành lời khuyên chung",
)

SKIPPED_TOPIC_TITLES = {
    "Cách đặt một câu hỏi dễ được cộng đồng giúp",
    "Một cuộc thảo luận hay bắt đầu từ ví dụ nào?",
    "Một câu chuyện nhỏ sau giờ học",
    "Cách chia sẻ kinh nghiệm thi mà không biến thành lời khuyên chung",
}

PRIMARY_TOPIC_EXCLUDE_MARKERS = (
    "công thức",
    "quy hoạch động",
    "tham lam",
    "điều luôn đúng",
    "invariant",
    "tối ưu",
    "lời giải mẫu",
    "editorial",
    "kỳ thi",
    "contest",
    "thuật toán",
    "trạng thái",
)

TOPIC_EXAMPLE_HINTS = (
    "Dùng ví dụ mảng cộng dồn: dãy 2, 5, 1, 4 và truy vấn tổng từ vị trí 2 đến 4.",
    "Dùng ví dụ đếm tần suất: dãy 2, 3, 2, 5, 3 và bảng đếm cnt.",
    "Dùng ví dụ xâu đối xứng: xâu abba và cách so sánh hai đầu.",
    "Dùng ví dụ tìm kiếm nhị phân: cắt dây dài 16, 12, 4 để được ít nhất 5 đoạn bằng nhau.",
    "Dùng ví dụ đồ thị nhỏ: 5 đỉnh, các cạnh 1-2, 2-3, 4-5 để thấy hai thành phần liên thông.",
    "Dùng ví dụ ba lô nhỏ: các vật có khối lượng 2, 3, 4 và sức chứa 5.",
    "Dùng ví dụ tiền xu: tạo tổng 11 từ các đồng 1, 5, 7.",
    "Dùng ví dụ số học: kiểm tra các ước của 18 hoặc ước chung lớn nhất của 24 và 36.",
    "Dùng ví dụ quy trình sau kỳ thi: một bài bị sai ở bộ kiểm thử nhỏ vì thiếu trường hợp mảng rỗng.",
)

PRIMARY_TOPIC_EXAMPLE_HINTS = (
    "Dùng ví dụ tính tiền mua 3 cây bút, mỗi cây 4 nghìn đồng.",
    "Dùng ví dụ đếm số lần xuất hiện của các số 1, 2, 1, 3, 2.",
    "Dùng ví dụ lập trình khối hoặc Python có biến tong_diem cộng điểm ba môn.",
    "Dùng ví dụ đọc đề có giá bút, số lượng bút, và một câu chuyện dài ở giữa.",
)

MATH_TOPIC_EXAMPLE_HINTS = (
    "Dùng ví dụ ước chung lớn nhất của 24 và 36 để thấy vì sao thuật toán Euclid giảm số rất nhanh.",
    "Dùng ví dụ tính chẵn lẻ: tổng của hai số lẻ luôn chẵn, thử với 3 + 5.",
    "Dùng ví dụ đếm ước của 18: 1, 2, 3, 6, 9, 18.",
    "Dùng ví dụ tổ hợp nhỏ: chọn 2 vị trí trong 4 vị trí, kết quả là 6 cách.",
)

ADVANCED_TOPIC_BANK = (
    "Từ một lời giải quốc tế, nên đọc điều luôn đúng nào trước?",
    "Khi nào nên dùng cấu trúc dữ liệu để tối ưu quy hoạch động?",
    "Một lỗi nhỏ trong bài khó: điều luôn đúng nhưng thứ tự duyệt sai",
    "Đọc lại một bài đồ thị khó bằng câu hỏi: cạnh nào thật sự thay đổi?",
)

ADVANCED_TOPIC_EXAMPLE_HINTS = (
    "Dùng ví dụ bài quốc tế trên cây: mỗi đỉnh lưu hai giá trị từ các con, rồi gộp kết quả khi quay lui bằng duyệt sâu.",
    "Dùng ví dụ tối ưu quy hoạch động: `dp[i]` lấy giá trị nhỏ nhất trên một đoạn trạng thái trước đó, và cần hỏi cấu trúc nào trả lời nhanh.",
    "Dùng ví dụ đồ thị trọng số 0/1: dùng hàng đợi hai đầu, cạnh trọng số 0 đẩy lên đầu, cạnh trọng số 1 đẩy xuống cuối.",
    "Dùng ví dụ truy vấn xử lý trước: sắp xếp truy vấn theo ngưỡng `k`, rồi thêm dần phần tử đủ điều kiện vào cây chỉ số nhị phân.",
)

TOPIC_SECTION_KEYWORDS = (
    (
        ("hỏi", "thắc mắc", "tro giup", "trợ giúp"),
        (
            "Cách đặt một câu hỏi dễ được cộng đồng giúp",
            "Một cuộc thảo luận hay bắt đầu từ ví dụ nào?",
            "Cách biến một bộ kiểm thử sai thành bài học ngắn",
        ),
    ),
    (
        ("tài liệu", "sách", "tutorial", "roadmap"),
        (
            "Chia sẻ tài liệu sao cho người sau dùng được ngay",
            "Một tài nguyên học thuật toán nên đọc chậm",
            "Đọc lời giải mẫu sao cho không mất ý tưởng chính",
        ),
    ),
    (
        ("kỳ thi", "contest", "voi", "ioi", "icpc", "hsg", "olympic"),
        (
            "Trước một kỳ thi, nên đọc lại lỗi cũ như thế nào?",
            "Nhìn lại một kỳ thi: nên học gì sau bảng điểm?",
            "Một danh sách kiểm tra trước khi bấm nút nộp bài",
        ),
    ),
    (
        ("off-topic", "tán gẫu", "học đường", "cuộc sống", "sở thích"),
        (
            "Một câu chuyện nhỏ sau giờ học",
            "Chia sẻ một câu chuyện học đường sao cho người khác muốn đọc",
            "Cách chia sẻ kinh nghiệm thi mà không biến thành lời khuyên chung",
        ),
    ),
)

PROBLEM_SYSTEM_PROMPT = r"""Bạn viết bài ngắn cho một chuyên mục cộng đồng trên LQDOJ.

Độc giả được mô tả trong AUDIENCE. Viết sao cho người chưa biết thuật ngữ vẫn theo được.

Mục tiêu: cho người đọc một chỗ bám để muốn thử bài. Không viết lời giải đầy đủ.

Định dạng bắt buộc:
1. Dòng đầu PHẢI đúng: **Bài gợi ý:** [PROBLEM_TITLE](PROBLEM_URL)
2. Dòng thứ hai PHẢI bắt đầu: **Tóm tắt:**
3. Sau đó viết nhiều đoạn ngắn: easy/standard dùng 3-5 đoạn; challenge/stretch dùng 5-8 đoạn.
4. Không dùng heading Markdown nào (`#`, `##`, `###`).

Ràng buộc:
- Không có giới hạn độ dài cứng.
- Bài dễ/trung bình nên gọn nếu ý tưởng đơn giản.
- Bài khó/rất khó có thể dài hơn nếu cần mô phỏng, công thức, hoặc danh sách kiểm tra khi cài đặt.
- Tránh tường chữ: chia thành đoạn ngắn, mỗi đoạn có một vai trò rõ.
- Có thể dùng 1-4 mẩu mã ngắn trong dấu `...` nếu giúp người đọc thấy cách cài, ví dụ `dfs(u)`, `visited[u] = true`, `set.insert(x)`.
- Với quy hoạch động, cấu trúc dữ liệu, số học, tham lam, tìm kiếm, đồ thị, đều có thể thêm mẫu rất ngắn như `dp[i] = ...`, `bit.add(i, x)`, `parent[x] = find(parent[x])`, `while l < r`, `cnt[x] += 1`.
- Không dùng khối mã và không chép lời giải đúng hoàn chỉnh vào bài.
- Không dùng HTML thô.
- Không link tuyệt đối; dùng chính PROBLEM_URL được cấp.
- Dùng chính xác PROBLEM_TITLE trong link đầu tiên.
- Ưu tiên tiếng Việt trong tiêu đề và câu văn. Thuật ngữ quen thuộc như DP, DFS, code, test, input, output, contest, editorial dùng được nếu tự nhiên; nếu dùng tên khó như Fenwick, giải thích ngay bằng tiếng Việt.
- Dùng LaTeX cho biến và công thức ngắn: $N$, $x$, $k$, $dp[i] = \max(dp[i], dp[j] + w_i)$.
- Với bài toán/quy hoạch động/số học, nên có 1-3 công thức ngắn nếu công thức giúp người đọc thấy trạng thái hoặc bước chuyển. Mỗi công thức phải được giải thích bằng lời ngay trước hoặc ngay sau.
- Không thả một khối công thức dài. Nếu công thức dài hơn một dòng, hãy tách thành lời.
- Không được nói “dùng công thức”, “tìm quy luật”, hoặc “rút gọn biểu thức” rồi không cho người đọc thấy công thức/quy luật đó.
- Với bài tiểu học hoặc bài dễ, nếu có công thức, phải dẫn bằng 2-3 số cụ thể trước rồi mới viết công thức ngắn.
- Khi viết công thức, lời giải thích phải khớp đúng phép toán trong công thức. Nếu công thức là $(a + b) \times k / 2$ thì lời phải nói cộng $a$ và $b$, không nói nhân $a$ với $b$.
- Với tổ hợp modulo, không viết “chia modulo”. Nếu cần nói kỹ, dùng “nghịch đảo modulo”; nếu không cần, chỉ nói tính với số nhỏ để kiểm tra công thức.
- Chỉ nhắc $O(\log N)$ hoặc độ phức tạp nếu thật cần để hiểu vì sao không duyệt trực tiếp.
- Đưa ví dụ nhỏ TRƯỚC khi gọi tên kỹ thuật.
- Nếu dùng thuật ngữ như `set`, DFS, thành phần liên thông, chặt nhị phân, chuẩn hóa, phải giải thích ngay bằng lời dễ hiểu trong cùng câu hoặc câu kế tiếp.
- Nên có một câu chuyển tự nhiên ở điểm đổi ý tưởng, dạng như: “Vậy cần cấu trúc dữ liệu nào trả lời được việc này?”, “Ta đang lặp lại phần nào?”, hoặc “Có đại lượng nào đủ nhỏ để lưu lại không?”.
- Câu chuyển phải xuất phát từ ví dụ/constraint vừa nói, không hỏi cho có.
- Bài nên mô phỏng dòng suy nghĩ tự nhiên của người giải: thử cách trực tiếp trên ví dụ nhỏ, nhận ra phần bị lặp hoặc phần cần nhớ, rồi mới đặt tên kỹ thuật.
- Không viết “suy nghĩ nội tâm” dài. Chỉ cần 2-4 câu hỏi/câu chuyển quan sát được, như một người đang giải thích trên blog.
- Có đúng một ví dụ nhỏ từ đề bài nếu có dữ liệu.
- Nếu đề bài có nhiều bước ví dụ liên tiếp, không được trộn trạng thái của bước này với bước khác. Giữ đúng từng lần cập nhật, hoặc viết “chẳng hạn” cho một ví dụ tự tạo khác.
- Nếu ví dụ là một dãy/hàng/bảng, nên viết ra vài phần tử đầu để người đọc thấy mẫu.
- Không phóng đại TLE nếu constraints nhỏ.
- Không dùng quá 2 câu liên tiếp mà không có ví dụ, biến, số cụ thể, hoặc thao tác cụ thể.
- Nếu bài hard hơn, phải dài hơn để giải thích từ từ. Đừng rút ngắn đến mức người mới chỉ thấy tên kỹ thuật.
- Bài nào cũng phải có “cơ chế chính”:
  - Ta theo dõi/lưu/tính đại lượng nào.
  - Đại lượng đó thay đổi như thế nào khi đi qua một bước của bài.
  - Vì sao làm vậy giúp tránh cách trực tiếp.
  - Khi cài đặt, thao tác cốt lõi là gì.
- Với bài khó/rất khó, bắt buộc có:
  - Một mô phỏng rất nhỏ bằng số cụ thể.
  - Một câu định nghĩa trạng thái hoặc dữ liệu cần lưu.
  - Một bước chuyển cụ thể: từ trạng thái cũ sang trạng thái mới, có thể viết bằng công thức ngắn.
  - Vì sao cách trực tiếp chậm, nhưng nói bằng quy mô của bài, không dọa chung chung.
  - Một checklist cài đặt ngắn ở cuối hoặc gần cuối.
- Nếu dùng cấu trúc dữ liệu hoặc chia căn, cơ chế chính thường là từ phần nhỏ đến toàn bài:
  - Mỗi khối/nút/bảng lưu gì.
  - Một truy vấn đọc dữ liệu đã lưu như thế nào.
  - Một cập nhật làm thay đổi phần nào và dựng lại/cập nhật phần nào.
  - Vì sao không cần dựng lại toàn bộ.
- Với bài cấu trúc dữ liệu khó, không được viết trạng thái mơ hồ như “từ nó đến hết” nếu không nói rõ phạm vi là trong khối, qua khối kế tiếp, hay toàn bộ dãy.
- Nếu chỉ đủ dữ kiện để gợi ý hướng giải, hãy nói như một hướng đọc bài: “một cách hay là lưu ...” thay vì khẳng định lời giải chi tiết chưa được giải thích.
- Nếu dùng quy hoạch động, phải có:
  - $dp[...]$ nghĩa là gì.
  - Bước chuyển lấy giá trị từ đâu.
  - Thứ tự duyệt hoặc điều kiện để chuyển đúng.
- Nếu dùng greedy, phải có:
  - Quyết định tham lam cụ thể ở một ví dụ nhỏ.
  - Lý do đổi sang quyết định khác không tốt hơn, nói bằng lời ngắn.

Kết bài:
- Không viết châm ngôn/lời khuyên chung.
- Không có “bài học là”, “hãy nhớ”, “điều quan trọng”.
- Không kết bằng một constraint hoặc thông tin đề bài đứng một mình, ví dụ “$|S| \le 255$”.
- Nếu nhắc constraint ở cuối, phải nói ngay constraint đó làm cách cài nào đủ dùng, ví dụ “vì $|S| \le 255$, chỉ cần `sort(digits)`”.
- Kết bằng một thao tác cụ thể người đọc sẽ làm trong bài, hoặc dừng ngay sau khi ý tưởng đã rõ.
- Câu cuối không cần mỹ từ. Ưu tiên một thao tác như “gọi `lower_bound`, thay giá trị ở vị trí tìm được, rồi lấy `tail.size()`”.

Văn phong:
- Thân thiện nhưng không trẻ con.
- Tự nhiên, ngắn, rõ.
- Không giáo trình hóa.
- Viết theo kiểu bài báo dễ đọc: mở bằng một cảnh/tình huống/ví dụ cụ thể, rồi mới mở rộng sang ý chính.
- Mỗi đoạn nên có 1-3 câu. Đoạn đầu sau tóm tắt phải đủ cụ thể để người đọc hình dung được ngay.
- Dùng nhịp “chi tiết cụ thể -> bối cảnh -> vì sao đáng chú ý -> bước tiếp theo”. Đừng mở đầu bằng nhận xét chung.
- Không bịa lời trích dẫn, tên người, sự kiện, số liệu, hoặc cảm xúc không có trong SOURCE_CONTEXT.
- Một câu chỉ nên chứa một ý.
- Ưu tiên động từ cụ thể: “đếm”, “đánh dấu”, “đi theo cạnh”, “cắt đoạn số”.
- Nếu bài có một thao tác cài đặt đáng học, nên có 1-2 mẩu mã ngắn trong dấu `...` để người đọc thấy thao tác chính.
- Với bài hard, nếu chưa chắc ý tưởng, hãy viết thận trọng: nêu phần chắc chắn từ đề, không bịa lời giải đầy đủ.
- Đừng chỉ gọi tên kỹ thuật rồi bỏ qua phần hay nhất. Người đọc cần thấy vì sao ý tưởng đó xuất hiện từ ví dụ.
- Với cấu trúc dữ liệu, phần hay nhất thường là điều luôn đúng: “mỗi khối lưu câu trả lời tạm nào” và “truy vấn nhảy qua khối ra sao”. Phải viết rõ điều đó.
- Với mọi chủ đề, phần hay nhất là cơ chế. Với số học là công thức đến từ đâu; với quy hoạch động là trạng thái chuyển ra sao; với tham lam là quyết định nào được giữ; với cài đặt là biến/mảng nào giúp tránh nhầm.
- Viết như một bài chuyên mục kỹ thuật nhỏ: có nhịp đọc, có khoảnh khắc “à, hóa ra cần lưu thứ này”, nhưng vẫn chính xác.
- Tránh danh từ trừu tượng khi có thể nói bằng ví dụ.
- Không dùng các cụm: trong hành trình, chinh phục, mổ xẻ, các bạn coder, hy vọng qua bài viết, bước lên sân khấu, thực chất, tối ưu hơn, hiệu quả hơn, nhiệm vụ của chúng ta, phản xạ tự nhiên, hoàn toàn chính xác, cách nghĩ đầu tiên của học sinh, chiếc hộp.

Tự kiểm trước khi trả lời:
- Câu cuối có phải châm ngôn/lời khuyên chung không? Nếu có, thay bằng chi tiết cụ thể.
- Câu cuối có chỉ là constraint/thông tin đề bài không? Nếu có, nối nó với một thao tác cài đặt cụ thể.
- Có heading Markdown không? Nếu có, xóa.
- Có cụm bị cấm không? Nếu có, viết lại.
- Link đầu tiên có đúng title và URL không?
- Có ví dụ trước thuật ngữ không?
- Bài có nói rõ cơ chế chính chưa: theo dõi gì, chuyển/cập nhật thế nào, và cài thao tác nào?
- Có câu nào quá dài không? Nếu có, tách câu.

Chỉ trả về Markdown cuối cùng."""

TOPIC_SYSTEM_PROMPT = r"""Bạn viết một bài chuyên mục ngắn cho cộng đồng LQDOJ.

Đây không phải bài giới thiệu tính năng, không phải giáo trình, không phải lời giải mẫu cho một bài cụ thể.
Mục tiêu là làm một chủ đề trong cộng đồng trở nên đáng đọc trong 3-7 đoạn ngắn.

Định dạng bắt buộc:
1. Dòng đầu PHẢI bắt đầu: **Tóm tắt:**
2. Không nhắc lại TOPIC_TITLE thành một dòng riêng. Tiêu đề bài đã nằm ở BlogPost.title.
3. Không dùng heading Markdown nào (`#`, `##`, `###`).

Ràng buộc:
- Không có giới hạn độ dài cứng.
- Không dùng khối mã.
- Có thể dùng vài mẩu mã ngắn trong dấu `...`.
- Có thể dùng 1-3 công thức ngắn nếu công thức giúp chủ đề rõ hơn.
- Đoạn đầu tiên sau tóm tắt phải dùng ngay ví dụ hoặc tình huống từ EXAMPLE_DIRECTION. Không mở bài bằng cảm giác chung chung.
- Phải có ví dụ nhỏ hoặc tình huống cụ thể trước khi gọi tên kỹ thuật/chiến thuật.
- Nếu là chủ đề thuật toán: phải có cơ chế chính, tức là theo dõi/lưu/tính gì, đại lượng đó thay đổi ra sao, và thao tác cài đặt cốt lõi là gì.
- Nếu là chủ đề học tập/cộng đồng/kỳ thi/tài liệu: phải có tình huống, quyết định cụ thể, và bước tiếp theo người đọc có thể làm ngay.
- Nên có một câu chuyển tự nhiên ở điểm đổi ý tưởng, ví dụ: “Ta đang làm lại phần nào?”, “Dữ liệu nào cần được ghi nhớ?”, “Cấu trúc nào trả lời được truy vấn này?”, hoặc “Người trả lời cần thêm thông tin gì để giúp mình?”.
- Bài nên mô phỏng dòng suy nghĩ tự nhiên của người học/người viết: thử trực tiếp, thấy chỗ vướng, chọn thứ cần lưu/hỏi/chia sẻ, rồi mới đặt tên kỹ thuật hoặc chiến thuật.
- Không nói lời khuyên chung chung.
- Mỗi bài chủ đề phải có một “mẩu làm việc” cụ thể: một dòng ghi chú mẫu, một bộ kiểm thử nhỏ, một công thức ngắn, một danh sách kiểm tra 2-3 bước, hoặc một ví dụ số.
- Nếu viết về đọc lời giải mẫu, phải có một ví dụ nhỏ về thứ cần chép lại, như `dp[i]` nghĩa là gì, thứ tự duyệt, hoặc một điều luôn đúng trong một dòng. Không chỉ nói “đọc ý chính”.
- Giữ cùng một ví dụ xuyên suốt bài. Nếu mở bằng lưới, các ghi chú sau cũng phải nói về lưới, ví dụ `dp[x][y] = dp[x-1][y] + dp[x][y-1]` hoặc $C(dx+dy, dx)$. Không chuyển đột ngột sang một bài toán khác.
- Không trộn hai kỹ thuật cho cùng một ví dụ ngắn, như vừa dùng tổ hợp $C(n,k)$ vừa chuyển sang quy hoạch động, trừ khi bài giải thích rõ vì sao hai cách nhìn tương đương.
- Với ví dụ lưới, chọn một hệ hướng duy nhất. Nếu đã nói “sang phải hoặc xuống dưới” thì không được gọi bước đó là “đi lên” ở câu sau.
- Nếu viết về toán, phải cho người đọc thấy ít nhất một phép tính nhỏ trước khi gọi tên công thức. Ví dụ: từ `(0,0)` đến `(2,1)` có 3 bước, chọn vị trí cho 1 bước đi lên nên có $C(3,1)=3$ cách.
- Nếu nhắc tính tổ hợp với modulo, phải nói “nghịch đảo modulo” hoặc giữ ví dụ ở số nhỏ. Không viết “chia modulo”.
- Nếu bài chỉ còn đúng các thao tác “mở tài liệu, ghi chú, đọc lại” mà không có ví dụ kỹ thuật nhỏ, hãy viết lại cho cụ thể hơn.
- Ví dụ phải hợp lý như một tình huống thật. Đừng viết những mẩu mã lạ như thể đó là toàn bộ bài; nếu nhắc mã, nói rõ đó là biến, dòng in để kiểm tra, hoặc đoạn xử lý nhỏ.
- Giữ một mạch lỗi/ý tưởng chính xuyên suốt. Đừng mở bằng BFS/lưới rồi chuyển sang tràn số nguyên nếu chưa giải thích mối liên hệ.
- Với bài cộng đồng/hỏi đáp, giọng phải tôn trọng người hỏi. Không chê “mã rối”, không nói người đọc sẽ bỏ đi ngay, không dùng hình ảnh nặng như “quăng một đống”.
- Với Hỏi đáp/Thắc mắc, ưu tiên giọng “bạn/mình” tự nhiên. Tránh dùng “ta” quá nhiều vì dễ thành giọng bài văn.
- Với chủ đề hỏi đáp, ví dụ tốt gồm: đường dẫn bài, dữ liệu vào nhỏ, kết quả thực tế, kết quả mong muốn, và đoạn xử lý nghi ngờ. Không bịa nguyên nhân kỹ thuật mơ hồ; nếu nói tràn số thì dùng “tràn số nguyên”, không viết “tràn bộ nhớ”.
- Với Hỏi đáp/Thắc mắc, không mở bài bằng tình huống hài quá tay hoặc câu cầu cứu phóng đại. Hãy viết như một học sinh đang hỏi nghiêm túc nhưng chưa biết trình bày rõ.
- Không ép từ thuật toán vào chủ đề đời sống/cộng đồng. Nếu viết cho Off-topic, Hỏi đáp, Tài liệu, hoặc Kỳ thi, dùng “điều cần chú ý”, “bước tiếp theo”, “chi tiết”, “quyết định”, không dùng “đại lượng”, “trạng thái”, “thao tác cài đặt” trừ khi thật sự nói về mã.
- Không dùng HTML thô.
- Ưu tiên tiếng Việt trong tiêu đề và câu văn. Thuật ngữ quen thuộc như DP, DFS, code, test, input, output, contest, editorial dùng được nếu tự nhiên; nếu dùng tên khó như Fenwick, giải thích ngay bằng tiếng Việt.

Văn phong:
- Tự nhiên, có nhịp đọc như một chuyên mục kỹ thuật nhỏ.
- Không giáo trình hóa.
- Viết theo kiểu bài báo dễ đọc: mở bằng một cảnh/tình huống/ví dụ cụ thể, rồi mới mở rộng sang ý chính.
- Mỗi đoạn nên có 1-3 câu. Đoạn đầu sau tóm tắt phải đủ cụ thể để người đọc hình dung được ngay.
- Dùng nhịp “chi tiết cụ thể -> bối cảnh -> vì sao đáng chú ý -> bước tiếp theo”. Đừng mở đầu bằng nhận xét chung.
- Không bịa lời trích dẫn, tên người, sự kiện, số liệu, hoặc cảm xúc không có trong SOURCE_CONTEXT.
- Câu ngắn. Nếu một câu có hơn 30 từ, hãy tách thành hai câu.
- Không kết bằng châm ngôn.
- Giữ giọng đời thường và chính xác. Không văn chương hóa quá mức, không làm căng cảm xúc bằng hình ảnh như “rêu phong”, “thước phim”, “góc khuất”.
- Không chê lỗi của người học là “ngớ ngẩn” hoặc “tư duy đứt gãy”. Nói lỗi cụ thể và cách kiểm tra.
- Không dùng các cụm: trong hành trình, chinh phục, mổ xẻ, các bạn coder, hy vọng qua bài viết, bước lên sân khấu, thực chất, tối ưu hơn, hiệu quả hơn, nhiệm vụ của chúng ta, phản xạ tự nhiên, hoàn toàn chính xác, cách nghĩ đầu tiên của học sinh, chiếc hộp, ngợp, ngộp, chia lấy dư, sa đà.

Chỉ trả về Markdown cuối cùng."""

CONTEST_SYSTEM_PROMPT = r"""Bạn viết một bài ngắn cho chuyên mục cộng đồng trên LQDOJ về một kỳ thi.

Đây KHÔNG phải lời giải đầy đủ. Mục tiêu là giúp người đọc biết kỳ thi có gì đáng thử và chọn 1-2 ý tưởng để bắt đầu.
Độc giả được mô tả trong AUDIENCE.

Định dạng bắt buộc:
1. Dòng đầu PHẢI đúng: **Kỳ thi:** [CONTEST_TITLE](CONTEST_URL)
2. Dòng thứ hai PHẢI bắt đầu: **Tóm tắt:**
3. Sau đó viết 3-5 đoạn ngắn.
4. Không dùng heading Markdown nào (`#`, `##`, `###`).

Ràng buộc:
- Không có giới hạn độ dài cứng, nhưng đừng thành một đoạn dài khó đọc.
- Viết theo kiểu bài báo dễ đọc: mở bằng một bài/tình huống cụ thể trong kỳ thi, rồi mới nói vì sao cả kỳ thi đáng đọc.
- Mỗi đoạn nên có 1-3 câu. Đoạn đầu sau tóm tắt phải đủ cụ thể để người đọc hình dung được ngay.
- Dùng nhịp “chi tiết cụ thể -> bối cảnh -> vì sao đáng chú ý -> bước tiếp theo”. Đừng mở đầu bằng nhận xét chung.
- Không bịa lời trích dẫn, tên người, sự kiện, số liệu, hoặc cảm xúc không có trong SOURCE_CONTEXT.
- Không dùng khối mã.
- Không dùng HTML thô.
- Không link tuyệt đối.
- Nhắc 2-4 bài trong kỳ thi bằng Markdown link nếu có.
- Không giải trọn kỳ thi.
- Nêu rõ đây là gợi ý đọc/ôn tập, không phải đáp án đầy đủ.
- Dùng ví dụ/tình huống cụ thể từ kỳ thi trước khi nói thuật ngữ.
- Có một câu chuyển tự nhiên: nên thử bài nào trước, hoặc bài nào giúp mở khóa ý tưởng nào.
- Nếu kỳ thi cơ bản, tránh từ nặng như điều luôn đúng, trạng thái, tối ưu hóa. Hãy nói bằng thao tác cụ thể: duyệt chỉ số nào, so sánh biến nào, cập nhật mảng nào.
- Không dùng câu mở kiểu quảng cáo hoặc câu đệm như “không gian nhẹ nhàng”, “khởi động tay chân”, “sa đà”. Vào thẳng bài đầu tiên nên thử và lý do.
- Ưu tiên tiếng Việt trong tiêu đề và câu văn. Thuật ngữ quen thuộc như DP, DFS, code, test, input, output, contest, editorial dùng được nếu tự nhiên; nếu dùng tên khó như Fenwick, giải thích ngay bằng tiếng Việt.

Kết bài:
- Không viết châm ngôn/lời khuyên chung.
- Kết bằng một gợi ý cụ thể: nên thử bài nào trước hoặc nên đọc đề theo thứ tự nào.

Chỉ trả về Markdown cuối cùng."""

REVIEW_SYSTEM_PROMPT = r"""Bạn là người duyệt bài chuyên mục cộng đồng LQDOJ.

Nhiệm vụ: đọc SOURCE_CONTEXT và DRAFT, rồi đánh giá liệu bài đã đủ tốt để đăng chưa.
Không viết lại toàn bộ bài. Chỉ trả JSON hợp lệ.
Hãy khó tính như biên tập viên. Nếu bài “ổn nhưng còn gượng”, publishable phải là false.
Điểm 9-10 chỉ dành cho bài có thể đăng ngay mà không cần sửa.

Tiêu chí:
- Đúng format Markdown bắt buộc.
- Không bịa chi tiết ngoài SOURCE_CONTEXT.
- Văn tự nhiên, không AI-like, không sáo rỗng.
- Câu dễ hiểu với học sinh đúng cấp. Nếu phải đọc lại mới hiểu, publishable phải là false.
- Có nhịp bài báo dễ đọc: chi tiết cụ thể trước, bối cảnh sau, rồi mới nói ý nghĩa hoặc bước tiếp theo.
- Đoạn đầu sau tóm tắt không được chung chung. Phải có cảnh/tình huống/ví dụ cụ thể.
- Không bịa lời trích dẫn, tên người, sự kiện, số liệu, hoặc cảm xúc không có trong SOURCE_CONTEXT.
- Không bắt lỗi các thuật ngữ quen thuộc như DP, DFS, code, test, input, output, contest, editorial nếu dùng tự nhiên.
- Nếu có tên như DFS, DP, Fenwick, phải giải thích ngay bằng tiếng Việt trong cùng câu hoặc câu kế tiếp.
- Có ví dụ/tình huống cụ thể trước thuật ngữ.
- Có chuyển ý tự nhiên như người đang giải thích cách nghĩ.
- Có cơ chế chính: theo dõi/hỏi/lưu/tính gì, thay đổi thế nào, bước tiếp theo là gì.
- Kết bài bằng thao tác cụ thể, không châm ngôn.
- Với bài cộng đồng/học tập, không đổ lỗi hoặc nói giọng phán xét người hỏi.
- Không dùng giọng chê bai như “quăng mã”, “đống hỗn độn”, “người đọc sẽ bỏ đi”.
- Ví dụ phải đúng thuật ngữ cơ bản; nếu nói bài tổng đoạn con thì không được viết nhầm thành tổng đoạn thẳng.
- Với bài hỏi đáp, nếu có lỗi kỹ thuật trong ví dụ, thuật ngữ phải đúng: “tràn số nguyên” khác “tràn bộ nhớ”.
- Không văn chương hóa quá mức ở Off-topic; ưu tiên chi tiết đời thường, không hình ảnh kịch tính.
- Không ép thuật ngữ thuật toán vào bài cộng đồng nếu chủ đề không nói về mã.
- Câu chuyển phải tự nhiên và có ích. Nếu câu hỏi tu từ nghe như được nhét vào cho đủ tiêu chí, hãy yêu cầu sửa.

Trả về đúng JSON:
{"publishable": true/false, "score": 1-10, "feedback": "1-3 góp ý cụ thể để sửa nếu chưa đạt"}

Chỉ trả JSON."""


@dataclass
class ProblemCandidate:
    code: str
    name: str
    url: str
    difficulty: str
    points: float
    user_count: int
    ac_rate: float
    group: str
    types: list
    statement: str
    source: str
    semantic_score: float = 0.0
    recent_users: int = 0
    recent_submissions: int = 0
    contest_name: str = ""


@dataclass
class GeneratedPost:
    title: str
    summary: str
    content: str
    candidate: ProblemCandidate = None
    contest: Contest = None
    topic: str = ""


@dataclass
class TopicExampleGuide:
    instruction: str
    required_markers: tuple
    label: str


@dataclass
class MagazineHistory:
    problem_codes: set
    contest_keys: set
    topic_titles: set
    normalized_titles: set


class Command(BaseCommand):
    help = "Generate randomized magazine posts for an organization"

    def add_arguments(self, parser):
        parser.add_argument("--org", default="tin-hoc-thcs", help="Organization slug")
        parser.add_argument("--author", default="admin", help="Author username")
        parser.add_argument("--count", type=int, default=3, help="Number of posts")
        parser.add_argument("--seed", default=None, help="Random seed")
        parser.add_argument(
            "--difficulty",
            choices=("random", "easy", "standard", "challenge", "stretch"),
            default="random",
        )
        parser.add_argument(
            "--post-type",
            choices=("problem", "contest", "topic", "mixed"),
            default="problem",
            help="Generate problem posts, topic columns, contest editorials, or mixed batch",
        )
        parser.add_argument(
            "--problem", default=None, help="Problem code to write about"
        )
        parser.add_argument("--contest", default=None, help="Contest key for editorial")
        parser.add_argument("--topic", default=None, help="Magazine topic title")
        parser.add_argument(
            "--update-post-id",
            type=int,
            default=None,
            help="Update one existing BlogPost instead of creating a new row",
        )
        parser.add_argument(
            "--evaluate-post-id",
            type=int,
            default=None,
            help="Run the reviewer on an existing BlogPost and exit",
        )
        parser.add_argument(
            "--commit",
            action="store_true",
            help="Create BlogPost rows. Without this, only prints generated posts.",
        )
        parser.add_argument(
            "--publish",
            action="store_true",
            help="Publish generated posts immediately. Defaults to pending review.",
        )
        parser.add_argument(
            "--llm",
            default="Gemini-3.5-Flash-Lite",
            help="Poe bot name to use for writing",
        )
        parser.add_argument(
            "--max-attempts",
            type=int,
            default=8,
            help="Maximum LLM write/rewrite attempts per post",
        )
        parser.add_argument(
            "--skip-review",
            action="store_true",
            help="Skip the LLM reviewer pass",
        )
        parser.add_argument(
            "--review-threshold",
            type=int,
            default=9,
            help="Minimum reviewer score required when review is enabled",
        )
        parser.add_argument(
            "--candidate-drafts",
            type=int,
            default=2,
            help="Number of reviewer-approved drafts to collect before choosing the best",
        )

    def handle(self, *args, **options):
        org = self._get_org(options["org"])
        self.target_org = org
        self.max_llm_attempts = max(1, options["max_attempts"])
        self.enable_llm_review = not options["skip_review"]
        self.review_threshold = min(max(options["review_threshold"], 1), 10)
        self.candidate_drafts = max(1, options["candidate_drafts"])
        author = self._get_author(options["author"])
        seed = options["seed"] or f"magazine-{org.slug}-{int(time.time())}"
        rng = random.Random(seed)
        self.stdout.write(f"seed={seed}")

        config = get_config()
        service = LLMService(
            api_key=config.api_key,
            bot_name=options["llm"],
            sleep_time=config.sleep_time,
            timeout=240,
        )

        if options["evaluate_post_id"]:
            self._evaluate_existing_post(service, options["evaluate_post_id"])
            return

        history = self._magazine_history(org, exclude_post_id=options["update_post_id"])
        used_codes = history.problem_codes
        post_type = options["post_type"]
        generated = []
        mixed_plan = (
            self._mixed_post_plan(options["count"], rng) if post_type == "mixed" else []
        )

        if post_type in ("problem", "mixed"):
            problem_count = (
                mixed_plan.count("problem")
                if post_type == "mixed"
                else options["count"]
            )
            if options["problem"]:
                difficulty = options["difficulty"]
                if difficulty == "random":
                    difficulty = self._choose_difficulties(rng, 1, "random", org)[0]
                if options["problem"] in used_codes:
                    raise CommandError(
                        f"Problem already appeared in this group: {options['problem']}"
                    )
                chosen = [
                    self._fixed_problem_candidate(options["problem"], difficulty, org)
                ]
            else:
                difficulties = self._choose_difficulties(
                    rng, problem_count, options["difficulty"], org
                )
                candidates = self._collect_problem_candidates(
                    difficulties, used_codes, org
                )
                chosen = self._choose_problem_candidates(rng, candidates, difficulties)
            for candidate in chosen:
                generated.append(self._generate_problem_post(service, candidate))

        if post_type == "contest" or "contest" in mixed_plan:
            contest = self._select_contest(rng, options["contest"], history)
            generated.append(self._generate_contest_post(service, contest))

        if post_type == "topic" or "topic" in mixed_plan:
            topic_count = (
                mixed_plan.count("topic") if post_type == "mixed" else options["count"]
            )
            used_topics = set()
            for _ in range(topic_count):
                if options["topic"]:
                    topic = options["topic"]
                    if self._normalized_title(topic) in history.topic_titles:
                        raise CommandError(
                            f"Topic already appeared in this group: {topic}"
                        )
                else:
                    topic = self._select_topic(rng, org, used_topics, history)
                used_topics.add(topic)
                generated.append(self._generate_topic_post(service, topic, org))

        if not generated:
            raise CommandError(
                "No magazine posts generated; no suitable candidates found"
            )

        if options["commit"]:
            if options["update_post_id"]:
                posts = [
                    self._update_post(
                        options["update_post_id"],
                        generated,
                        org,
                        author,
                        visible=options["publish"],
                    )
                ]
            else:
                posts = self._commit_posts(
                    generated,
                    org,
                    author,
                    visible=options["publish"],
                )
            for post in posts:
                self.stdout.write(
                    self.style.SUCCESS(
                        f"saved id={post.id} url={post.get_absolute_url()} title={post.title}"
                    )
                )
        else:
            self.stdout.write("dry-run: no BlogPost rows created")

        for item in generated:
            self.stdout.write("=" * 72)
            self.stdout.write(item.title)
            self.stdout.write(item.content)

    def _get_org(self, slug):
        try:
            return Organization.objects.get(slug=slug)
        except Organization.DoesNotExist as exc:
            raise CommandError(f"Organization not found: {slug}") from exc

    def _get_author(self, username):
        try:
            return User.objects.get(username=username).profile
        except User.DoesNotExist as exc:
            raise CommandError(f"Author user not found: {username}") from exc

    def _evaluate_existing_post(self, service, post_id):
        try:
            post = BlogPost.objects.get(id=post_id)
        except BlogPost.DoesNotExist as exc:
            raise CommandError(f"BlogPost not found: {post_id}") from exc

        org_text = "\n".join(
            self._organization_text(org) for org in post.organizations.all()
        )
        source_context = (
            f"POST_TITLE: {post.title}\n"
            f"POST_SUMMARY: {post.summary}\n"
            f"ORGANIZATION_CONTEXT:\n{org_text or 'Không có organization context.'}"
        )
        review = self._review_body(service, source_context, post.content)
        status = "PASS" if review["passed"] else "FAIL"
        self.stdout.write(
            f"{status} id={post.id} score={review['score']} feedback={review['feedback']}"
        )

    def _magazine_history(self, org, exclude_post_id=None):
        queryset = BlogPost.objects.filter(organizations=org)
        if exclude_post_id:
            queryset = queryset.exclude(id=exclude_post_id)
        rows = queryset.values("title", "content", "summary")
        problem_codes = set()
        contest_keys = set()
        topic_titles = set()
        normalized_titles = set()
        for row in rows:
            title = row["title"] or ""
            summary = row["summary"] or ""
            content = row["content"] or ""
            problem_codes.update(PROBLEM_LINK_RE.findall(content))
            contest_keys.update(CONTEST_LINK_RE.findall(content))

            normalized_title = self._normalized_title(title)
            if normalized_title:
                normalized_titles.add(normalized_title)

            for topic in TOPIC_LINE_RE.findall(content):
                normalized_topic = self._normalized_title(topic)
                if normalized_topic:
                    topic_titles.add(normalized_topic)

            if summary.startswith("Magazine topic:") or summary.startswith(
                "Chủ đề chuyên mục:"
            ):
                normalized_topic = self._normalized_title(
                    summary.split(":", 1)[1].strip()
                )
                if normalized_topic:
                    topic_titles.add(normalized_topic)

        return MagazineHistory(
            problem_codes=problem_codes,
            contest_keys=contest_keys,
            topic_titles=topic_titles,
            normalized_titles=normalized_titles,
        )

    def _normalized_title(self, value):
        value = strip_tags(value or "").lower()
        value = re.sub(r"^\[magazine[^\]]*\]\s*", "", value)
        value = re.sub(r"\s+", " ", value)
        value = re.sub(r"[^\wÀ-ỹ ]+", "", value)
        return value.strip()

    def _choose_difficulties(self, rng, count, requested, org=None):
        if requested != "random":
            return [requested] * count
        level = self._audience_level(org)
        difficulty_weights = DIFFICULTY_WEIGHTS_BY_LEVEL.get(level, DIFFICULTY_WEIGHTS)
        names = list(difficulty_weights)
        weights = [difficulty_weights[name] for name in names]
        difficulties = rng.choices(names, weights=weights, k=count)
        if count >= 3 and len(set(difficulties)) == 1 and len(names) >= 3:
            difficulties[1] = names[min(1, len(names) - 1)]
            difficulties[2] = names[min(2, len(names) - 1)]
        return difficulties

    def _mixed_post_plan(self, count, rng):
        if count <= 0:
            return []
        base = ["problem", "topic", "contest"]
        if count == 1:
            return rng.choices(base, weights=(45, 45, 10), k=1)
        if count <= len(base):
            return rng.sample(base, count)
        plan = list(base)
        plan.extend(rng.choice(("problem", "topic")) for _ in range(count - len(base)))
        rng.shuffle(plan)
        return plan

    def _collect_problem_candidates(self, difficulties, used_codes, org):
        candidates = []
        seen = set(used_codes)
        for difficulty in set(difficulties):
            candidates.extend(self._semantic_problem_candidates(difficulty, seen, org))
            candidates.extend(self._activity_problem_candidates(difficulty, seen, org))
            candidates.extend(
                self._recent_contest_problem_candidates(difficulty, seen, org)
            )
        return candidates

    def _semantic_problem_candidates(self, difficulty, seen, org):
        if not getattr(settings, "USE_ML", False):
            return []
        results = []
        low, high = self._difficulty_range(difficulty, org)
        for query in self._queries_for_org(difficulty, org):
            try:
                rows = search_problems(query, limit=25)
            except SemanticSearchUnavailable:
                continue
            codes = [row["code"] for row in rows]
            scores = {row["code"]: row.get("score", 0.0) for row in rows}
            queryset = self._base_problem_queryset().filter(
                code__in=codes, points__gte=low, points__lte=high
            )
            for problem in queryset:
                candidate = self._candidate_from_problem(
                    problem,
                    difficulty,
                    "semantic",
                    org=org,
                    semantic_score=scores.get(problem.code, 0.0),
                )
                if candidate and problem.code not in seen:
                    results.append(candidate)
                    seen.add(problem.code)
        return results

    def _activity_problem_candidates(self, difficulty, seen, org):
        low, high = self._difficulty_range(difficulty, org)
        cutoff = timezone.now() - timedelta(days=14)
        rows = (
            Submission.objects.filter(
                date__gte=cutoff,
                problem__is_public=True,
                problem__is_organization_private=False,
                problem__points__gte=low,
                problem__points__lte=high,
            )
            .values("problem_id")
            .annotate(
                recent_submissions=Count("id"),
                recent_users=Count("user_id", distinct=True),
                last_submission=Max("date"),
            )
            .filter(recent_users__gte=5)
            .order_by("-recent_users", "-recent_submissions")[:40]
        )
        problem_ids = [row["problem_id"] for row in rows]
        row_map = {row["problem_id"]: row for row in rows}
        results = []
        for problem in self._base_problem_queryset().filter(id__in=problem_ids):
            if problem.code in seen:
                continue
            row = row_map[problem.id]
            candidate = self._candidate_from_problem(
                problem,
                difficulty,
                "trending",
                org=org,
                recent_users=row["recent_users"],
                recent_submissions=row["recent_submissions"],
            )
            if candidate:
                results.append(candidate)
                seen.add(problem.code)
        return results

    def _recent_contest_problem_candidates(self, difficulty, seen, org):
        low, high = self._difficulty_range(difficulty, org)
        cutoff = timezone.now() - timedelta(days=45)
        contest_ids = list(
            Contest.objects.filter(
                is_visible=True,
                is_organization_private=False,
                end_time__lte=timezone.now(),
                end_time__gte=cutoff,
            ).values_list("id", flat=True)[:10]
        )
        rows = (
            ContestProblem.objects.filter(
                contest_id__in=contest_ids,
                problem__is_public=True,
                problem__is_organization_private=False,
                problem__points__gte=low,
                problem__points__lte=high,
            )
            .select_related("contest", "problem", "problem__group")
            .prefetch_related("problem__types")
            .order_by("-contest__end_time", "order")[:40]
        )
        results = []
        for row in rows:
            problem = row.problem
            if problem.code in seen:
                continue
            candidate = self._candidate_from_problem(
                problem,
                difficulty,
                "recent_contest",
                org=org,
                contest_name=row.contest.name,
            )
            if candidate:
                results.append(candidate)
                seen.add(problem.code)
        return results

    def _base_problem_queryset(self):
        return (
            Problem.objects.filter(
                is_public=True,
                is_organization_private=False,
                description__gt="",
                user_count__gte=10,
            )
            .select_related("group")
            .prefetch_related("types")
        )

    def _queries_for_org(self, difficulty, org):
        return DIFFICULTY_QUERIES[difficulty]

    def _difficulty_range(self, difficulty, org):
        level = self._audience_level(org)
        level_ranges = DIFFICULTY_RANGES_BY_LEVEL.get(level, {})
        return level_ranges.get(difficulty, DIFFICULTY_RANGES[difficulty])

    def _candidate_from_problem(self, problem, difficulty, source, **kwargs):
        org = kwargs.get("org")
        if not self._audience_problem_ok(problem, org):
            return None
        statement = self._clean_statement(problem.description)
        if not self._good_statement(statement):
            return None
        return ProblemCandidate(
            code=problem.code,
            name=problem.name,
            url=f"/problem/{problem.code}",
            difficulty=difficulty,
            points=problem.points,
            user_count=problem.user_count,
            ac_rate=round(problem.ac_rate, 2),
            group=problem.group.full_name if problem.group else "",
            types=[item.full_name for item in problem.types.all()],
            statement=statement[:1800],
            source=source,
            semantic_score=float(kwargs.get("semantic_score", 0.0)),
            recent_users=int(kwargs.get("recent_users", 0)),
            recent_submissions=int(kwargs.get("recent_submissions", 0)),
            contest_name=kwargs.get("contest_name", ""),
        )

    def _fixed_problem_candidate(self, code, difficulty, org):
        try:
            problem = (
                Problem.objects.filter(
                    is_public=True,
                    is_organization_private=False,
                    description__gt="",
                )
                .select_related("group")
                .prefetch_related("types")
                .get(code=code)
            )
        except Problem.DoesNotExist as exc:
            raise CommandError(f"Problem not found or not eligible: {code}") from exc

        candidate = self._candidate_from_problem(
            problem,
            difficulty,
            "fixed_problem",
            org=org,
        )
        if not candidate:
            raise CommandError(
                f"Problem does not fit target audience or statement: {code}"
            )
        return candidate

    def _audience_problem_ok(self, problem, org):
        if not org:
            return True

        type_text = " ".join(item.full_name for item in problem.types.all())
        group_name = problem.group.full_name if problem.group else ""
        haystack = f"{problem.name} {group_name} {type_text}".lower()
        level = self._audience_level(org)

        if level == "primary":
            return not any(marker in haystack for marker in PRIMARY_EXCLUDE_MARKERS)
        if level == "middle":
            return not any(marker in haystack for marker in THCS_EXCLUDE_MARKERS)
        if level == "high":
            return not any(marker in haystack for marker in THPT_EXCLUDE_MARKERS)
        if level == "advanced":
            return not any(marker in haystack for marker in VOI_EXCLUDE_MARKERS)

        return True

    def _audience_level(self, org):
        text = self._organization_text(org).lower()
        primary_markers = ("tiểu học", "tieu hoc", "thta", "tht a", "bảng a")
        middle_markers = ("thcs", "trung học cơ sở", "trung hoc co so", "thtb", "tht b")
        high_markers = (
            "thpt",
            "trung học phổ thông",
            "trung hoc pho thong",
            "thtc",
            "tht c",
        )
        advanced_markers = (
            "voi",
            "ioi",
            "quốc gia",
            "quoc gia",
            "đội tuyển",
            "doi tuyen",
        )
        if any(marker in text for marker in primary_markers):
            return "primary"
        if any(marker in text for marker in advanced_markers):
            return "advanced"
        if any(marker in text for marker in middle_markers):
            return "middle"
        if any(marker in text for marker in high_markers):
            return "high"
        return "general"

    def _organization_text(self, org):
        if not org:
            return ""
        return " ".join(
            item
            for item in (org.name, org.short_name, strip_tags(org.about or ""))
            if item
        )

    def _clean_statement(self, description):
        text = strip_tags(description or "").replace("\r", "")
        return "\n".join(line.strip() for line in text.splitlines() if line.strip())

    def _good_statement(self, statement):
        if len(statement) < 220:
            return False
        lower = statement.lower()
        non_self_contained_markers = (
            "đề bài nằm",
            "de bai nam",
            "xem tại",
            "xem tai",
            "tham khảo",
            "tham khao",
            "http://",
            "https://",
            "i've reviewed",
            "already in good",
            "formatting follows",
        )
        if any(marker in lower for marker in non_self_contained_markers):
            return False
        image_markers = (
            statement.count("![")
            + statement.count("enter image")
            + statement.count("Screenshot")
        )
        return not (image_markers >= 2 and len(statement) < 900)

    def _choose_problem_candidates(self, rng, candidates, difficulties):
        chosen = []
        used = set()
        for difficulty in difficulties:
            pool = [
                candidate
                for candidate in candidates
                if candidate.difficulty == difficulty and candidate.code not in used
            ]
            if not pool:
                pool = [
                    candidate for candidate in candidates if candidate.code not in used
                ]
            if not pool:
                break
            pool.sort(key=self._candidate_score, reverse=True)
            top = pool[: min(8, len(pool))]
            weights = [max(self._candidate_score(candidate), 0.1) for candidate in top]
            candidate = rng.choices(top, weights=weights, k=1)[0]
            chosen.append(candidate)
            used.add(candidate.code)
        return chosen

    def _candidate_score(self, candidate):
        hardness = 1.0 - min(max(candidate.ac_rate, 5), 85) / 100.0
        popularity = min(candidate.user_count, 1000) / 1000.0
        recent = min(candidate.recent_users, 200) / 200.0
        semantic = candidate.semantic_score
        return semantic * 2.0 + hardness * 0.8 + popularity * 0.3 + recent * 0.4

    def _generate_problem_post(self, service, candidate):
        title = self._problem_post_title(candidate)
        prompt = self._problem_prompt(candidate)
        content = self._call_with_validation(
            service,
            prompt,
            PROBLEM_SYSTEM_PROMPT,
            lambda body: self._validate_problem_post(body, candidate),
        )
        return GeneratedPost(
            title=title,
            summary=f"{candidate.difficulty}: {candidate.name}",
            content=content,
            candidate=candidate,
        )

    def _select_topic(self, rng, org, used_topics=None, history=None):
        topics = list(DEFAULT_TOPIC_BANK)
        org_text = self._organization_text(org).lower()
        for keywords, section_topics in TOPIC_SECTION_KEYWORDS:
            if any(keyword in org_text for keyword in keywords):
                topics.extend(section_topics)
                topics.extend(section_topics)
        if self._audience_level(org) == "advanced":
            topics.extend(ADVANCED_TOPIC_BANK)
            topics.extend(ADVANCED_TOPIC_BANK)
        topics = self._filter_topics_for_org(
            topics,
            org,
            used_topics or set(),
            history or MagazineHistory(set(), set(), set(), set()),
        )
        if not topics:
            raise CommandError(
                "No suitable magazine topics found for this organization"
            )
        return rng.choice(topics)

    def _filter_topics_for_org(self, topics, org, used_topics, history):
        level = self._audience_level(org)
        filtered = []
        seen = set()
        normalized_used_topics = {
            self._normalized_title(topic) for topic in used_topics if topic
        }
        for topic in topics:
            normalized_topic = self._normalized_title(topic)
            if (
                topic in seen
                or normalized_topic in normalized_used_topics
                or normalized_topic in history.topic_titles
                or normalized_topic in history.normalized_titles
                or topic in SKIPPED_TOPIC_TITLES
            ):
                continue
            seen.add(topic)
            lower = topic.lower()
            if level == "primary" and any(
                marker in lower for marker in PRIMARY_TOPIC_EXCLUDE_MARKERS
            ):
                continue
            filtered.append(topic)
        return filtered

    def _generate_topic_post(self, service, topic, org):
        guide = self._topic_example_guide(topic, org)
        prompt = self._topic_prompt(topic, org, guide)
        content = self._call_with_validation(
            service,
            prompt,
            TOPIC_SYSTEM_PROMPT,
            lambda body: self._validate_topic_post(body, topic, guide),
        )
        return GeneratedPost(
            title=topic[:100],
            summary=f"Chủ đề chuyên mục: {topic}",
            content=content,
            topic=topic,
        )

    def _topic_prompt(self, topic, org, guide):
        return f"""TOPIC_TITLE: {topic}
AUDIENCE:
{self._audience_prompt_text(None)}
EXAMPLE_DIRECTION:
{guide.instruction}

Viết một bài chuyên mục ngắn theo thứ tự:
1. Bắt đầu bằng **Tóm tắt:** trong một câu ngắn.
2. Mở đoạn thân bài đầu tiên bằng một tình huống cụ thể.
3. Cho ngay ví dụ nhỏ trong EXAMPLE_DIRECTION: số/xâu/dãy nếu là thuật toán, hoặc một tình huống thật nếu là học tập/cộng đồng.
4. Chỉ ra điều cần chú ý: dữ liệu cần lưu, câu hỏi cần hỏi, lỗi cần ghi lại, hoặc chi tiết cần chọn.
5. Nói điều đó thay đổi hoặc giúp quyết định bước tiếp theo như thế nào.
6. Cho một mẩu làm việc thật nhỏ: một bộ kiểm thử, một dòng ghi chú mẫu, một công thức, một biến/điều luôn đúng, hoặc danh sách kiểm tra 2-3 bước.
7. Nêu bước thực hiện cụ thể. Chỉ dùng mẩu mã trong dấu `...` nếu thật sự đang nói về mã.
8. Kết bằng một thao tác cụ thể người đọc có thể thử, không kết luận chung chung.

Không viết dòng **Chủ đề:** TOPIC_TITLE trong nội dung. Tiêu đề đã nằm ngoài bài.

Bắt buộc dùng đúng EXAMPLE_DIRECTION làm ví dụ chính của bài. Không thay bằng ví dụ khác tiện tay hơn."""

    def _topic_example_guide(self, topic, org):
        level = self._audience_level(org)
        if level == "primary":
            hints = self._primary_topic_example_guides()
        elif level == "advanced":
            hints = self._advanced_topic_example_guides()
            if "ioi" in topic.lower():
                return hints[0]
        elif org and org.slug == "toan-hoc-trong-lap-trinh":
            hints = self._math_topic_example_guides()
        else:
            hints = self._default_topic_example_guides()
        seed_text = f"{org.slug if org else ''}:{topic}"
        index = sum(
            (position + 1) * ord(char) for position, char in enumerate(seed_text)
        )
        index %= len(hints)
        guide = hints[index]
        if isinstance(guide, TopicExampleGuide):
            return guide
        return TopicExampleGuide(
            instruction=(
                f"{guide['instruction']} Không tự động dùng lại ví dụ đường đi trên lưới "
                "nếu gợi ý này đã đưa ra một ngữ cảnh khác."
            ),
            required_markers=guide["required_markers"],
            label=guide["label"],
        )

    def _default_topic_example_guides(self):
        return (
            {
                "instruction": TOPIC_EXAMPLE_HINTS[0],
                "required_markers": (
                    "2, 5, 1, 4",
                    "cộng dồn",
                    "truy vấn tổng",
                ),
                "label": "mảng cộng dồn",
            },
            {
                "instruction": TOPIC_EXAMPLE_HINTS[1],
                "required_markers": ("2, 3, 2, 5, 3", "cnt", "tần suất", "bảng đếm"),
                "label": "đếm tần suất",
            },
            {
                "instruction": TOPIC_EXAMPLE_HINTS[2],
                "required_markers": ("abba", "đối xứng", "hai đầu"),
                "label": "xâu đối xứng",
            },
            {
                "instruction": TOPIC_EXAMPLE_HINTS[3],
                "required_markers": ("16", "12", "4", "5 đoạn", "nhị phân"),
                "label": "tìm kiếm nhị phân",
            },
            {
                "instruction": TOPIC_EXAMPLE_HINTS[4],
                "required_markers": ("1-2", "2-3", "4-5", "thành phần liên thông"),
                "label": "đồ thị nhỏ",
            },
            {
                "instruction": TOPIC_EXAMPLE_HINTS[5],
                "required_markers": ("2, 3, 4", "sức chứa 5", "ba lô", "dp"),
                "label": "ba lô nhỏ",
            },
            {
                "instruction": TOPIC_EXAMPLE_HINTS[6],
                "required_markers": ("11", "1, 5, 7", "đồng", "dp"),
                "label": "tiền xu",
            },
            {
                "instruction": TOPIC_EXAMPLE_HINTS[7],
                "required_markers": ("18", "24", "36", "ước chung", "ước"),
                "label": "số học nhỏ",
            },
            {
                "instruction": TOPIC_EXAMPLE_HINTS[8],
                "required_markers": (
                    "WA",
                    "bộ kiểm thử nhỏ",
                    "mảng rỗng",
                    "n = 0",
                    "`n=0`",
                ),
                "label": "quy trình sau kỳ thi",
            },
        )

    def _primary_topic_example_guides(self):
        return (
            {
                "instruction": PRIMARY_TOPIC_EXAMPLE_HINTS[0],
                "required_markers": ("3", "4", "bút", "nghìn"),
                "label": "tính tiền mua bút",
            },
            {
                "instruction": PRIMARY_TOPIC_EXAMPLE_HINTS[1],
                "required_markers": ("1, 2, 1, 3, 2", "đếm", "xuất hiện"),
                "label": "đếm số lần xuất hiện",
            },
            {
                "instruction": PRIMARY_TOPIC_EXAMPLE_HINTS[2],
                "required_markers": ("tong_diem", "ba môn", "cộng điểm"),
                "label": "biến cộng điểm",
            },
            {
                "instruction": PRIMARY_TOPIC_EXAMPLE_HINTS[3],
                "required_markers": ("giá bút", "số lượng", "câu chuyện"),
                "label": "lọc dữ liệu trong đề",
            },
        )

    def _math_topic_example_guides(self):
        return (
            {
                "instruction": MATH_TOPIC_EXAMPLE_HINTS[0],
                "required_markers": ("24", "36", "Euclid", "ước chung"),
                "label": "gcd Euclid",
            },
            {
                "instruction": MATH_TOPIC_EXAMPLE_HINTS[1],
                "required_markers": ("3 + 5", "lẻ", "chẵn", "tính chẵn lẻ"),
                "label": "chẵn lẻ",
            },
            {
                "instruction": MATH_TOPIC_EXAMPLE_HINTS[2],
                "required_markers": ("18", "1, 2, 3, 6, 9", "ước"),
                "label": "đếm ước",
            },
            {
                "instruction": MATH_TOPIC_EXAMPLE_HINTS[3],
                "required_markers": ("chọn 2", "4 vị trí", "6 cách", "tổ hợp"),
                "label": "tổ hợp nhỏ",
            },
        )

    def _advanced_topic_example_guides(self):
        return (
            TopicExampleGuide(
                instruction=(
                    f"{ADVANCED_TOPIC_EXAMPLE_HINTS[0]} Không tự động dùng lại ví dụ "
                    "đường đi trên lưới nếu gợi ý này đã đưa ra một ngữ cảnh khác."
                ),
                required_markers=("duyệt sâu", "cây", "gộp", "quay lui", "hai giá trị"),
                label="điều luôn đúng trên cây",
            ),
            TopicExampleGuide(
                instruction=(
                    f"{ADVANCED_TOPIC_EXAMPLE_HINTS[1]} Không tự động dùng lại ví dụ "
                    "đường đi trên lưới nếu gợi ý này đã đưa ra một ngữ cảnh khác."
                ),
                required_markers=("dp[i]", "nhỏ nhất", "đoạn", "trạng thái"),
                label="tối ưu quy hoạch động bằng cấu trúc dữ liệu",
            ),
            TopicExampleGuide(
                instruction=(
                    f"{ADVANCED_TOPIC_EXAMPLE_HINTS[2]} Không tự động dùng lại ví dụ "
                    "đường đi trên lưới nếu gợi ý này đã đưa ra một ngữ cảnh khác."
                ),
                required_markers=("hàng đợi hai đầu", "0", "1", "trọng số"),
                label="0-1 BFS",
            ),
            TopicExampleGuide(
                instruction=(
                    f"{ADVANCED_TOPIC_EXAMPLE_HINTS[3]} Không tự động dùng lại ví dụ "
                    "đường đi trên lưới nếu gợi ý này đã đưa ra một ngữ cảnh khác."
                ),
                required_markers=(
                    "truy vấn xử lý trước",
                    "k",
                    "cây chỉ số nhị phân",
                    "sắp xếp",
                ),
                label="truy vấn xử lý trước bằng cây chỉ số nhị phân",
            ),
        )

    def _validate_topic_post(self, body, topic, guide):
        errors = self._common_markdown_errors(body)
        lines = body.strip().splitlines()
        if not lines or not lines[0].strip().startswith("**Tóm tắt:**"):
            errors.append("Dòng đầu của bài chủ đề phải bắt đầu bằng **Tóm tắt:**")
        if lines and lines[0].strip().startswith("**Chủ đề:**"):
            errors.append("Không nhắc lại tiêu đề bằng dòng **Chủ đề:** trong nội dung")
        if "```" in body:
            errors.append(
                "Không dùng khối mã trong bài chuyên mục; chỉ dùng mẩu mã ngắn trong dấu `...`"
            )
        errors.extend(self._readability_errors(body))
        errors.extend(self._ending_errors(body))
        errors.extend(self._topic_core_mechanism_errors(body))
        errors.extend(self._topic_specificity_errors(body))
        errors.extend(self._topic_example_guide_errors(body, guide))
        return errors

    def _topic_example_guide_errors(self, body, guide):
        lower = self._body_without_code_blocks(body).lower()
        if not any(marker.lower() in lower for marker in guide.required_markers):
            return [
                f"Bài chưa dùng đúng ví dụ chính từ EXAMPLE_DIRECTION: {guide.label}"
            ]
        if guide.label != "tổ hợp nhỏ" and any(
            marker in lower
            for marker in ("đường đi trên lưới", "trên lưới", "sang phải", "xuống dưới")
        ):
            return [
                "Bài đang tự chuyển sang ví dụ lưới dù EXAMPLE_DIRECTION yêu cầu ví dụ khác"
            ]
        return []

    def _problem_post_title(self, candidate):
        return candidate.name

    def _problem_prompt(self, candidate):
        return f"""PROBLEM_TITLE: {candidate.name}
PROBLEM_URL: {candidate.url}
DIFFICULTY_MODE: {candidate.difficulty}
SOURCE: {candidate.source}
POINTS: {candidate.points}
AC_RATE: {candidate.ac_rate}
TYPES: {', '.join(candidate.types)}
STATEMENT:
{candidate.statement}

Hãy tự chọn một ví dụ nhỏ từ đề bài nếu có.
AUDIENCE:
{self._audience_prompt_text(candidate)}

Viết một bài ngắn theo thứ tự:
1. Tóm tắt bài bằng lời dễ hiểu.
2. Một ví dụ nhỏ.
3. Từ ví dụ đó, chỉ ra thứ nên lưu hoặc nên theo dõi.
4. Nói cách trực tiếp sẽ vướng gì bằng constraint cụ thể của bài.
5. Gọi tên kỹ thuật sau khi đã có nhu cầu rõ ràng, rồi giải thích thuật ngữ bằng lời thường.
6. Với bài challenge/stretch, thêm một bước chuyển cụ thể hoặc một checklist cài đặt ngắn.
7. Dừng ở một thao tác cụ thể của bài, không kết luận chung chung."""

    def _audience_prompt_text(self, candidate):
        org = getattr(self, "target_org", None)
        if org:
            level = self._audience_level(org)
            level_hints = {
                "primary": "Ưu tiên câu ngắn, ví dụ thật cụ thể, rất ít thuật ngữ.",
                "middle": "Có thể có bài thử thách, nhưng phải đi từ ví dụ cụ thể trước.",
                "high": "Có thể dùng thuật ngữ thuật toán, nhưng vẫn cần giải thích mạch suy nghĩ.",
                "advanced": "Có thể dùng thuật ngữ nâng cao, nhưng phải nói rõ điều luôn đúng/trạng thái/bước chuyển.",
                "general": "Điều chỉnh độ sâu theo điểm bài và mô tả cộng đồng.",
            }
            about = strip_tags(org.about or "").strip()
            if len(about) > 700:
                about = about[:700] + "..."
            return (
                f"Cộng đồng: {org.name} ({org.slug}).\n"
                f"Mô tả/about: {about or 'Không có mô tả.'}\n"
                f"Gợi ý mức đọc: {level_hints[level]}"
            )
        if "tiểu học" in candidate.group.lower() or "bảng a" in candidate.group.lower():
            return "Học sinh tiểu học hoặc Tin học trẻ bảng A. Cần câu ngắn, ví dụ thật cụ thể, thuật ngữ rất ít."
        if candidate.difficulty in ("challenge", "stretch"):
            return "Học sinh đang luyện nghiêm túc. Có thể giải thích dài hơn, nhưng vẫn phải đi từ ví dụ cụ thể trước."
        return "Học sinh phổ thông trong cộng đồng được chọn. Giải thích vừa đủ, tránh quá nhiều thuật ngữ."

    def _validate_problem_post(self, body, candidate):
        errors = self._common_markdown_errors(body)
        first_line = f"**Bài gợi ý:** [{candidate.name}]({candidate.url})"
        lines = body.strip().splitlines()
        if not lines or lines[0].strip() != first_line:
            errors.append(f"Dòng đầu phải đúng: {first_line}")
        if not self._has_summary_after_first_line(lines):
            errors.append("Dòng nội dung đầu sau link phải bắt đầu bằng **Tóm tắt:**")
        if "```" in body:
            errors.append(
                "Không dùng khối mã trong bài chuyên mục; chỉ dùng mẩu mã ngắn trong dấu `...`"
            )
        errors.extend(self._code_block_errors(body))
        errors.extend(self._readability_errors(body))
        errors.extend(self._ending_errors(body))
        errors.extend(self._formula_reference_errors(body, candidate))
        errors.extend(self._hard_post_detail_errors(body, candidate))
        errors.extend(self._core_mechanism_errors(body, candidate))
        return errors

    def _select_contest(self, rng, key, history):
        queryset = Contest.objects.filter(
            is_visible=True, is_organization_private=False
        )
        if key:
            try:
                contest = queryset.get(key=key)
            except Contest.DoesNotExist as exc:
                raise CommandError(f"Visible public contest not found: {key}") from exc
            if contest.key in history.contest_keys:
                raise CommandError(
                    f"Contest already appeared in this group: {contest.key}"
                )
            return contest
        cutoff = timezone.now() - timedelta(days=45)
        contests = list(
            queryset.filter(end_time__lte=timezone.now(), end_time__gte=cutoff)
            .annotate(problem_count=Count("contest_problems"))
            .filter(problem_count__gte=2)
            .order_by("-end_time")[:12]
        )
        contests = [
            contest for contest in contests if contest.key not in history.contest_keys
        ]
        if not contests:
            raise CommandError("No recent visible public contest found")
        return rng.choice(contests[: min(6, len(contests))])

    def _generate_contest_post(self, service, contest):
        rows = (
            ContestProblem.objects.filter(contest=contest, problem__isnull=False)
            .select_related("problem", "problem__group")
            .prefetch_related("problem__types")
            .order_by("order")[:8]
        )
        problems = []
        for row in rows:
            problem = row.problem
            problems.append(
                {
                    "title": problem.name,
                    "url": f"/problem/{problem.code}",
                    "points": problem.points,
                    "types": [item.full_name for item in problem.types.all()],
                    "statement": self._clean_statement(problem.description)[:700],
                }
            )
        prompt = self._contest_prompt(contest, problems)
        content = self._call_with_validation(
            service,
            prompt,
            CONTEST_SYSTEM_PROMPT,
            lambda body: self._validate_contest_post(body, contest),
        )
        return GeneratedPost(
            title=f"Gợi ý đọc kỳ thi: {contest.name}",
            summary=f"Gợi ý đọc kỳ thi: {contest.name}",
            content=content,
            contest=contest,
        )

    def _contest_prompt(self, contest, problems):
        return f"""CONTEST_TITLE: {contest.name}
CONTEST_URL: /contest/{contest.key}
CONTEST_DESCRIPTION:
{self._clean_statement(contest.description)[:1000]}
PROBLEMS_JSON:
{json.dumps(problems, ensure_ascii=False, indent=2)}

AUDIENCE:
{self._audience_prompt_text(None)}

Viết bài gợi ý đọc kỳ thi cho cộng đồng trên. Không giải trọn kỳ thi."""

    def _validate_contest_post(self, body, contest):
        errors = self._common_markdown_errors(body)
        if "```" in body:
            errors.append("Không dùng khối mã trong bài kỳ thi")
        first_line = f"**Kỳ thi:** [{contest.name}](/contest/{contest.key})"
        lines = body.strip().splitlines()
        if not lines or lines[0].strip() != first_line:
            errors.append(f"Dòng đầu phải đúng: {first_line}")
        if not self._has_summary_after_first_line(lines):
            errors.append("Dòng nội dung đầu sau link phải bắt đầu bằng **Tóm tắt:**")
        return errors

    def _has_summary_after_first_line(self, lines):
        for line in lines[1:]:
            if not line.strip():
                continue
            return line.startswith("**Tóm tắt:**")
        return False

    def _lines_after_summary(self, lines):
        for index, line in enumerate(lines):
            if line.strip().startswith("**Tóm tắt:**"):
                return lines[index + 1 :]
        return lines[2:] if len(lines) > 2 else []

    def _common_markdown_errors(self, body):
        errors = []
        body_without_code = self._body_without_code_blocks(body)
        lower_body = body_without_code.lower()
        lines = body_without_code.strip().splitlines()
        if any(line.startswith("#") for line in lines):
            errors.append("Không dùng heading Markdown")
        if "https://lqdoj.edu.vn" in body:
            errors.append("Không dùng link tuyệt đối")
        if body.count("$") % 2:
            errors.append("LaTeX trong dòng bị thiếu dấu $ đóng/mở")
        if body.count("`") % 2:
            errors.append("Mẩu mã trong dấu `...` bị thiếu dấu ` đóng/mở")
        for phrase in BANNED_PHRASES:
            if phrase.lower() in lower_body:
                errors.append(f"Có cụm bị cấm: {phrase}")
        errors.extend(self._language_errors(body_without_code))
        if ("xuống dưới" in lower_body or "bước xuống" in lower_body) and (
            "lên trên" in lower_body or "bước đi lên" in lower_body
        ):
            errors.append(
                "Ví dụ lưới bị lẫn hướng lên/xuống; hãy dùng một hệ hướng nhất quán"
            )
        repeated = re.search(r"\b([\wÀ-ỹ]{3,}\s+[\wÀ-ỹ]{3,})\s+\1\b", lower_body)
        if repeated:
            errors.append(f"Có cụm bị lặp: {repeated.group(1)}")
        return errors

    def _prose_only_text(self, body):
        text = self._body_without_code_blocks(body)
        text = re.sub(r"`[^`]*`", "", text)
        text = re.sub(r"\[[^\]]+\]\([^)]+\)", "", text)
        text = re.sub(r"/(?:problem|contest)/[-a-z0-9_]+", "", text)
        lines = text.strip().splitlines()
        return "\n".join(self._lines_after_summary(lines))

    def _language_errors(self, body):
        errors = []
        prose = self._prose_only_text(body)
        lower = prose.lower()
        found_terms = [
            term
            for term in ENGLISH_PROSE_TERMS
            if re.search(rf"(?<![a-z]){re.escape(term)}(?![a-z])", lower)
        ]
        if found_terms:
            errors.append(
                "Tránh từ tiếng Anh kiểu meta/gượng trong câu văn: "
                + ", ".join(sorted(set(found_terms))[:5])
            )

        sentences = self._split_sentences(prose)
        for term, explanations in UNEXPLAINED_TECHNICAL_TERMS.items():
            for index, sentence in enumerate(sentences):
                if not re.search(
                    rf"(?<![a-z]){re.escape(term)}(?![a-z])", sentence.lower()
                ):
                    continue
                window = " ".join(sentences[index : index + 2]).lower()
                if not any(explanation in window for explanation in explanations):
                    errors.append(
                        f"Nếu nhắc {term.upper()}, phải giải thích ngay bằng tiếng Việt"
                    )
                    break

        return errors

    def _code_block_errors(self, body):
        errors = []
        blocks = list(self._iter_code_blocks(body))
        if len(blocks) > 2:
            errors.append("Chỉ dùng tối đa 2 khối mã ngắn")
            return errors
        if not blocks:
            return errors

        languages = {language.lower() for language, _ in blocks if language}
        allowed = {"python", "py", "cpp", "c++"}
        if any(language and language.lower() not in allowed for language, _ in blocks):
            errors.append("Khối mã chỉ nên dùng Python/C++")
        if len(blocks) == 2 and not (
            languages.intersection({"python", "py"})
            and languages.intersection({"cpp", "c++"})
        ):
            errors.append("Nếu dùng 2 khối mã, nên là một Python và một C++")

        total_lines = 0
        for _, code in blocks:
            lines = [line for line in code.splitlines() if line.strip()]
            total_lines += len(lines)
            if len(lines) > 10:
                errors.append("Mỗi khối mã chỉ nên dài tối đa 10 dòng")
        if total_lines > 18:
            errors.append("Ví dụ mã quá dài; chỉ giữ thao tác chính")
        return errors

    def _iter_code_blocks(self, body):
        pattern = re.compile(
            r"```([A-Za-z0-9_+#-]*)\s*\n(.*?)\n\s*```",
            re.DOTALL,
        )
        for match in pattern.finditer(body):
            yield match.group(1), match.group(2)

    def _body_without_code_blocks(self, body):
        return re.sub(
            r"```[A-Za-z0-9_+#-]*\s*\n.*?\n\s*```",
            "",
            body,
            flags=re.DOTALL,
        )

    def _readability_errors(self, body):
        errors = []
        body = self._body_without_code_blocks(body)
        lines = [line.strip() for line in body.strip().splitlines()]
        content_lines = self._lines_after_summary(lines)
        body_after_summary = "\n".join(content_lines)
        paragraphs = [
            line
            for line in content_lines
            if line and not line.startswith("**Bài gợi ý:**")
        ]
        if len(paragraphs) < 3:
            errors.append("Cần ít nhất 3 đoạn sau phần tóm tắt")

        errors.extend(self._opening_style_errors(paragraphs))

        sentences = self._split_sentences(" ".join(paragraphs))
        long_sentences = [
            sentence for sentence in sentences if len(sentence.split()) > 44
        ]
        if long_sentences:
            errors.append("Có câu quá dài; mỗi câu nên dưới 44 từ")

        dense_sentences = [
            sentence
            for sentence in sentences
            if len(sentence.split()) > 30 and sentence.count(",") >= 3
        ]
        if dense_sentences:
            errors.append(
                "Có câu quá dày ý; hãy tách thành câu ngắn hơn để học sinh dễ theo"
            )

        hard_term_sentences = 0
        for sentence in sentences:
            lower_sentence = sentence.lower()
            hits = sum(1 for word in ABSTRACT_WORDS if word in lower_sentence) + len(
                re.findall(r"`[^`]+`|\$[^$]+\$", sentence)
            )
            if hits >= 4:
                hard_term_sentences += 1
        if hard_term_sentences:
            errors.append(
                "Có câu gom quá nhiều thuật ngữ/ký hiệu; cần giải thích chậm hơn"
            )

        abstract_count = sum(body_after_summary.count(word) for word in ABSTRACT_WORDS)
        if abstract_count > 6:
            errors.append(
                "Quá nhiều từ trừu tượng; cần dùng ví dụ và động từ cụ thể hơn"
            )

        first_technical = self._first_technical_term_position(body_after_summary)
        first_example = self._first_example_position(body_after_summary)
        if first_technical is not None and (
            first_example is None or first_technical < first_example
        ):
            errors.append("Cần đưa ví dụ nhỏ trước khi gọi tên kỹ thuật")

        return errors

    def _opening_style_errors(self, paragraphs):
        if not paragraphs:
            return []

        first = paragraphs[0].lower()
        has_concrete_marker = (
            bool(re.search(r"`[^`]+`|\$[^$]+\$|\b\d+\b", paragraphs[0]))
            or any(marker in first for marker in CONCRETE_OPENING_MARKERS)
            or " - " in paragraphs[0]
        )
        generic_openers = (
            "bài viết này",
            "chủ đề này",
            "bài toán này",
            "kỳ thi này",
            "trong bài viết này",
            "khi học thuật toán",
            "đây là một",
            "có một điều",
        )
        if not has_concrete_marker or first.startswith(generic_openers):
            return [
                "Đoạn đầu sau tóm tắt cần mở bằng cảnh/tình huống/ví dụ cụ thể, không mở chung chung"
            ]
        return []

    def _ending_errors(self, body):
        errors = []
        body = self._body_without_code_blocks(body)
        paragraphs = [
            line.strip() for line in body.strip().splitlines() if line.strip()
        ]
        if not paragraphs:
            return errors

        last = paragraphs[-1]
        if self._looks_like_bare_constraint_ending(last):
            errors.append(
                "Câu cuối không nên chỉ là constraint/thông tin đề bài; hãy nối với thao tác cài đặt cụ thể"
            )
        return errors

    def _looks_like_bare_constraint_ending(self, text):
        lower = text.lower()
        constraint_markers = (
            "tối đa",
            "tối thiểu",
            "không quá",
            "không vượt quá",
            "giới hạn",
            "\\le",
            "\\ge",
            "<=",
            ">=",
        )
        action_markers = (
            "`",
            "sort",
            "sắp xếp",
            "quét",
            "duyệt",
            "đếm",
            "cập nhật",
            "đánh dấu",
            "lưu",
            "gom",
            "tách",
            "dùng",
            "chọn",
            "tính",
        )
        return any(marker in lower for marker in constraint_markers) and not any(
            marker in lower for marker in action_markers
        )

    def _hard_post_detail_errors(self, body, candidate):
        if candidate.difficulty not in ("challenge", "stretch"):
            return []

        errors = []
        body_without_code = self._body_without_code_blocks(body)
        paragraphs = [
            line.strip()
            for line in body_without_code.strip().splitlines()[2:]
            if line.strip()
        ]
        lower = body_without_code.lower()

        if len(paragraphs) < 5:
            errors.append("Bài challenge/stretch cần ít nhất 5 đoạn ngắn sau tóm tắt")
        if not any(
            marker in lower for marker in ("gọi", "lưu", "theo dõi", "đặt", "dp[")
        ):
            errors.append("Bài hard cần nói rõ trạng thái/dữ liệu sẽ lưu")
        if not any(
            marker in lower
            for marker in ("từ", "sang", "chuyển", "cập nhật", "nhân", "cộng thêm")
        ):
            errors.append("Bài hard cần có một bước chuyển cụ thể")
        if not any(
            marker in lower
            for marker in ("cài", "viết", "quét", "duyệt", "sort", "while", "`")
        ):
            errors.append("Bài hard cần có thao tác cài đặt cụ thể")
        return errors

    def _core_mechanism_errors(self, body, candidate):
        body_without_code = self._body_without_code_blocks(body)
        lower = body_without_code.lower()
        errors = []

        if candidate.difficulty in ("standard", "challenge", "stretch"):
            track_markers = (
                "theo dõi",
                "lưu",
                "đếm",
                "tính",
                "gọi",
                "đặt",
                "mảng",
                "biến",
                "$dp",
                "dp[",
                "cnt",
            )
            change_markers = (
                "chuyển",
                "cập nhật",
                "tăng",
                "giảm",
                "cộng",
                "nhân",
                "thay",
                "sang",
                "từ",
                "quét",
                "duyệt",
            )
            implementation_markers = (
                "`",
                "cài",
                "viết",
                "vòng lặp",
                "sort",
                "sắp xếp",
                "duyệt",
                "quét",
                "mảng",
                "biến",
            )
            if not any(marker in lower for marker in track_markers):
                errors.append("Bài cần nói rõ ta theo dõi/lưu/tính đại lượng nào")
            if not any(marker in lower for marker in change_markers):
                errors.append("Bài cần nói rõ đại lượng đó thay đổi/cập nhật thế nào")
            if not any(marker in lower for marker in implementation_markers):
                errors.append("Bài cần có thao tác cài đặt cốt lõi")

        return errors

    def _topic_core_mechanism_errors(self, body):
        body_without_code = self._body_without_code_blocks(body)
        lower = body_without_code.lower()
        errors = []
        track_markers = (
            "theo dõi",
            "lưu",
            "đếm",
            "tính",
            "gọi",
            "đặt",
            "mảng",
            "biến",
            "$dp",
            "dp[",
            "cnt",
            "hỏi",
            "ghi chú",
            "tài liệu",
            "lỗi",
            "bộ kiểm thử",
            "ví dụ",
            "thông tin",
        )
        change_markers = (
            "chuyển",
            "cập nhật",
            "tăng",
            "giảm",
            "cộng",
            "nhân",
            "thay",
            "sang",
            "từ",
            "quét",
            "duyệt",
            "vướng",
            "thiếu",
            "sửa",
            "chọn",
            "quyết định",
            "trả lời",
            "chia sẻ",
        )
        implementation_markers = (
            "`",
            "cài",
            "viết",
            "vòng lặp",
            "sort",
            "sắp xếp",
            "duyệt",
            "quét",
            "mảng",
            "biến",
            "bước",
            "thử",
            "đọc lại",
            "ghi lại",
            "đăng",
            "hỏi",
            "trả lời",
            "chia sẻ",
        )
        if not any(marker in lower for marker in track_markers):
            errors.append(
                "Bài topic cần nói rõ đối tượng cụ thể đang được theo dõi/hỏi/ghi lại"
            )
        if not any(marker in lower for marker in change_markers):
            errors.append(
                "Bài topic cần nói rõ điều gì thay đổi, vướng ở đâu, hoặc quyết định nào được đưa ra"
            )
        if not any(marker in lower for marker in implementation_markers):
            errors.append("Bài topic cần có bước tiếp theo cụ thể người đọc có thể làm")
        return errors

    def _topic_specificity_errors(self, body):
        body_without_code = self._body_without_code_blocks(body)
        lower = body_without_code.lower()
        errors = []

        concrete_signals = 0
        concrete_signals += len(re.findall(r"`[^`]+`", body_without_code))
        concrete_signals += len(re.findall(r"\$[^$]+\$", body_without_code))
        concrete_signals += len(re.findall(r"\b\d+\b", body_without_code))
        concrete_signals += sum(
            1
            for marker in (
                "ví dụ",
                "chẳng hạn",
                "danh sách kiểm tra",
                "bước 1",
                "bước đầu",
                "dữ liệu vào",
                "kết quả ra",
                "bộ kiểm thử",
            )
            if marker in lower
        )
        if concrete_signals < 3:
            errors.append(
                "Bài chủ đề cần có mẩu làm việc cụ thể hơn: bộ kiểm thử, công thức, ghi chú mẫu, hoặc danh sách kiểm tra ngắn"
            )

        advice_only_markers = (
            "mở một tờ giấy",
            "mở một file ghi chú",
            "đọc lại",
            "viết lại ý tưởng",
        )
        if (
            sum(1 for marker in advice_only_markers if marker in lower) >= 2
            and concrete_signals < 5
        ):
            errors.append(
                "Bài topic đang nghiêng về lời khuyên; cần thêm ví dụ kỹ thuật nhỏ trước bước ghi chú"
            )
        if "bfs" in lower and "tràn số nguyên" in lower:
            errors.append(
                "Bài đang trộn ví dụ BFS/lưới với lỗi tràn số nguyên; hãy giữ một lỗi hoặc một cơ chế chính xuyên suốt"
            )
        return errors

    def _formula_reference_errors(self, body, candidate):
        body_without_code = self._body_without_code_blocks(body)
        lower = body_without_code.lower()
        formula_words = (
            "công thức",
            "quy luật",
            "rút gọn biểu thức",
            "biểu thức",
        )
        has_formula_word = any(word in lower for word in formula_words)
        if not has_formula_word:
            return []

        has_visible_formula = bool(
            re.search(
                r"\$[^$]*(=|\\sum|\\max|\\min|\\cdot|\\times|\\frac)[^$]*\$",
                body_without_code,
            )
        )
        has_inline_step = any(
            marker in body_without_code
            for marker in ("=", "+", "-", "\\times", "\\cdot", "`")
        )
        if has_visible_formula or has_inline_step:
            return []

        return [
            "Đã nhắc công thức/quy luật nhưng chưa cho người đọc thấy công thức hoặc bước tính cụ thể"
        ]

    def _split_sentences(self, text):
        return [
            sentence.strip()
            for sentence in re.split(r"(?<=[.!?。！？])\s+", text)
            if sentence.strip()
        ]

    def _first_technical_term_position(self, body):
        positions = [
            body.find(term)
            for term in (
                "DFS",
                "set",
                "chặt nhị phân",
                "thành phần liên thông",
                "chuẩn hóa",
                "độ phức tạp",
            )
            if body.find(term) != -1
        ]
        return min(positions) if positions else None

    def _first_example_position(self, body):
        markers = ("Ví dụ", "Chẳng hạn", "`", "$N =", "$x =")
        positions = [body.find(marker) for marker in markers if body.find(marker) != -1]
        return min(positions) if positions else None

    def _call_with_validation(self, service, prompt, system_prompt, validate):
        feedback = ""
        approved = []
        for _ in range(self.max_llm_attempts):
            full_prompt = prompt
            if feedback:
                full_prompt += (
                    f"\n\nLần trước chưa đạt: {feedback}\nHãy trả lại bản đã sửa."
                )
            body = service.call_llm(full_prompt, system_prompt=system_prompt)
            if not body:
                feedback = "Không có phản hồi"
                continue
            body = body.strip() + "\n"
            errors = validate(body)
            if errors:
                feedback = "; ".join(errors)
                self.stdout.write(self.style.WARNING(f"validation failed: {feedback}"))
                continue

            if not getattr(self, "enable_llm_review", True):
                return body

            review = self._review_body(service, prompt, body)
            if review["passed"]:
                approved.append((review["score"], body))
                self.stdout.write(
                    self.style.SUCCESS(
                        f"review passed: score={review['score']} "
                        f"candidate={len(approved)}/{self.candidate_drafts}"
                    )
                )
                if len(approved) >= self.candidate_drafts:
                    approved.sort(key=lambda item: item[0], reverse=True)
                    return approved[0][1]
                feedback = (
                    "Bản trước đã đạt. Hãy viết một biến thể khác tự nhiên hơn, "
                    "giữ đúng format và tránh lặp lại câu chữ."
                )
                continue

            feedback = f"Reviewer chưa duyệt: {review['feedback']}"
            self.stdout.write(self.style.WARNING(f"validation failed: {feedback}"))
        if approved:
            approved.sort(key=lambda item: item[0], reverse=True)
            return approved[0][1]
        raise CommandError(f"LLM output failed validation: {feedback}")

    def _review_body(self, service, source_context, body):
        if not getattr(self, "enable_llm_review", True):
            return {"passed": True, "score": 10, "feedback": ""}

        review_prompt = f"""SOURCE_CONTEXT:
{source_context[:6000]}

DRAFT:
{body}
"""
        response = service.call_llm(
            review_prompt,
            system_prompt=REVIEW_SYSTEM_PROMPT,
        )
        if not response:
            return {
                "passed": False,
                "score": 0,
                "feedback": "Reviewer không trả phản hồi",
            }

        review = self._parse_review_response(response)
        if not review:
            return {
                "passed": False,
                "score": 0,
                "feedback": "Reviewer không trả JSON hợp lệ",
            }

        score = int(review.get("score") or 0)
        publishable = bool(review.get("publishable"))
        feedback = self._review_feedback_text(review.get("feedback"))
        return {
            "passed": publishable and score >= self.review_threshold,
            "score": score,
            "feedback": f"score={score}; {feedback}",
        }

    def _review_feedback_text(self, feedback):
        if feedback is None:
            return ""
        if isinstance(feedback, list):
            return "; ".join(
                str(item).strip() for item in feedback if str(item).strip()
            )
        if isinstance(feedback, dict):
            return "; ".join(f"{key}: {value}" for key, value in feedback.items())
        return str(feedback).strip()

    def _parse_review_response(self, response):
        response = response.strip()
        try:
            return json.loads(response)
        except json.JSONDecodeError:
            pass

        match = re.search(r"\{.*\}", response, re.DOTALL)
        if not match:
            return None
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None

    def _commit_posts(self, generated, org, author, visible=False):
        created = []
        with revisions.create_revision():
            for item in generated:
                post = BlogPost.objects.create(
                    title=item.title[:100],
                    slug=slugify(item.title)[:50],
                    visible=visible,
                    sticky=False,
                    publish_on=timezone.now(),
                    content=item.content,
                    summary=item.summary,
                    is_organization_private=True,
                    is_rejected=False,
                )
                post.authors.add(author)
                post.organizations.add(org)
                created.append(post)
            revisions.set_comment("Generated magazine posts")
            revisions.set_user(author.user)
        return created

    def _update_post(self, post_id, generated, org, author, visible=False):
        if len(generated) != 1:
            raise CommandError("--update-post-id requires exactly one generated post")
        item = generated[0]
        try:
            post = BlogPost.objects.get(id=post_id)
        except BlogPost.DoesNotExist as exc:
            raise CommandError(f"BlogPost not found: {post_id}") from exc

        with revisions.create_revision():
            post.title = item.title[:100]
            post.slug = slugify(item.title)[:50]
            post.visible = visible
            post.is_rejected = False
            post.publish_on = timezone.now()
            post.content = item.content
            post.summary = item.summary
            post.is_organization_private = True
            post.save()
            post.authors.add(author)
            post.organizations.add(org)
            revisions.set_comment("Updated generated magazine post")
            revisions.set_user(author.user)
        return post
