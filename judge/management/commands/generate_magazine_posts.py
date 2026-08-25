import json
import random
import re
import time
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections
from django.db.models import Count, Max, Q
from django.utils import timezone
from django.utils.html import strip_tags
from django.utils.text import slugify
from django.utils.translation import gettext as _

from asgiref.sync import async_to_sync, sync_to_async
import fastapi_poe as fp
from reversion import revisions

from judge.ml.semantic_search import SemanticSearchUnavailable, search_problems
from judge.models import (
    BlogPost,
    Contest,
    ContestProblem,
    Organization,
    Problem,
    ProblemSolutionCode,
    Solution,
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
    "Dùng ví dụ đồ thị trọng số 0/1: dùng deque (hàng đợi hai đầu), cạnh trọng số 0 đẩy lên đầu, cạnh trọng số 1 đẩy xuống cuối.",
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
- Có thể dùng “code” hoặc “mã” theo ngữ cảnh; không ép thay một từ bằng từ kia.
- Có thể dùng `stack`, `queue`, và `deque` hoặc tên tiếng Việt tương ứng theo ngữ cảnh; không ép dịch thuật ngữ quen thuộc.
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
- Viết theo lối wiki giáo dục dễ đọc: định nghĩa đối tượng trong 1-2 câu, cho ngay một ví dụ nhỏ, rồi mới nói cách trực tiếp vướng ở đâu.
- Khi giới thiệu cấu trúc dữ liệu hoặc kỹ thuật, hãy nêu “mỗi phần lưu gì” và “một thao tác đọc/cập nhật dùng thông tin đó ra sao”.
- Nếu có nhiều hướng giải, chỉ so sánh 2 hướng rõ nhất: cách trực tiếp và cách đáng thử hơn. Không liệt kê dài.
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
Tự kiểm trước khi trả lời:
- Câu cuối có phải châm ngôn/lời khuyên chung không? Nếu có, thay bằng chi tiết cụ thể.
- Câu cuối có chỉ là constraint/thông tin đề bài không? Nếu có, nối nó với một thao tác cài đặt cụ thể.
- Có heading Markdown không? Nếu có, xóa.
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
- Giữ cùng một ví dụ xuyên suốt bài. Nếu mở bằng mảng thì các ghi chú sau cũng phải nói về mảng; nếu mở bằng đồ thị thì các ghi chú sau cũng phải nói về đồ thị. Không chuyển đột ngột sang một bài toán khác.
- Không trộn hai kỹ thuật cho cùng một ví dụ ngắn, như vừa dùng tổ hợp $C(n,k)$ vừa chuyển sang quy hoạch động, trừ khi bài giải thích rõ vì sao hai cách nhìn tương đương.
- Nếu viết về toán, phải cho người đọc thấy ít nhất một phép tính nhỏ trước khi gọi tên công thức. Ví dụ: chọn 2 vị trí trong 4 vị trí thì có $C(4,2)=6$ cách.
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
- Có thể dùng “code” hoặc “mã” theo ngữ cảnh; không ép thay một từ bằng từ kia.
- Có thể dùng `stack`, `queue`, và `deque` hoặc tên tiếng Việt tương ứng theo ngữ cảnh; không ép dịch thuật ngữ quen thuộc.

Văn phong:
- Tự nhiên, có nhịp đọc như một chuyên mục kỹ thuật nhỏ.
- Không giáo trình hóa.
- Viết theo kiểu bài báo dễ đọc: mở bằng một cảnh/tình huống/ví dụ cụ thể, rồi mới mở rộng sang ý chính.
- Viết theo lối wiki giáo dục dễ đọc: định nghĩa đối tượng trong 1-2 câu, cho ngay một ví dụ nhỏ, rồi mới nói cách trực tiếp vướng ở đâu.
- Khi giới thiệu cấu trúc dữ liệu hoặc kỹ thuật, hãy nêu “mỗi phần lưu gì” và “một thao tác đọc/cập nhật dùng thông tin đó ra sao”.
- Nếu có nhiều hướng giải, chỉ so sánh 2 hướng rõ nhất: cách trực tiếp và cách đáng thử hơn. Không liệt kê dài.
- Mỗi đoạn nên có 1-3 câu. Đoạn đầu sau tóm tắt phải đủ cụ thể để người đọc hình dung được ngay.
- Dùng nhịp “chi tiết cụ thể -> bối cảnh -> vì sao đáng chú ý -> bước tiếp theo”. Đừng mở đầu bằng nhận xét chung.
- Không bịa lời trích dẫn, tên người, sự kiện, số liệu, hoặc cảm xúc không có trong SOURCE_CONTEXT.
- Câu ngắn. Nếu một câu có hơn 30 từ, hãy tách thành hai câu.
- Không kết bằng châm ngôn.
- Giữ giọng đời thường và chính xác. Không văn chương hóa quá mức, không làm căng cảm xúc bằng hình ảnh như “rêu phong”, “thước phim”, “góc khuất”.
- Không chê lỗi của người học là “ngớ ngẩn” hoặc “tư duy đứt gãy”. Nói lỗi cụ thể và cách kiểm tra.
Chỉ trả về Markdown cuối cùng."""

CONTEST_SYSTEM_PROMPT = r"""Bạn viết một bài chuyên mục cộng đồng trên LQDOJ về một kỳ thi.

Mục tiêu là tạo một bài đánh giá và phân tích ngắn, có thông tin cụ thể về kỳ thi.
Độc giả được mô tả trong AUDIENCE.

Định dạng bắt buộc:
1. Dòng đầu PHẢI đúng: **Kỳ thi:** [CONTEST_TITLE](CONTEST_URL)
2. Dòng thứ hai PHẢI bắt đầu: **Tóm tắt:**
3. Sau đó viết các đoạn ngắn, rõ ràng.
4. Không dùng heading Markdown nào (`#`, `##`, `###`).

Ràng buộc:
- Không có giới hạn độ dài cứng, nhưng đừng thành một đoạn dài khó đọc.
- Viết theo kiểu bài báo dễ đọc: mở bằng một bài/tình huống cụ thể trong kỳ thi, rồi mới nói vì sao cả kỳ thi đáng đọc.
- Viết theo lối wiki giáo dục dễ đọc: mô tả bài đầu tiên thật cụ thể, nêu cách trực tiếp người đọc dễ nghĩ tới, rồi gợi ý vì sao cần một ý tưởng tốt hơn.
- Nếu nhắc kỹ thuật của một bài, giải thích bằng ví dụ hoặc thao tác nhỏ trước khi gọi tên kỹ thuật.
- Mỗi đoạn nên có 1-3 câu. Đoạn đầu sau tóm tắt phải đủ cụ thể để người đọc hình dung được ngay.
- Dùng nhịp “chi tiết cụ thể -> bối cảnh -> vì sao đáng chú ý -> bước tiếp theo”. Đừng mở đầu bằng nhận xét chung.
- Không bịa lời trích dẫn, tên người, sự kiện, số liệu, hoặc cảm xúc không có trong SOURCE_CONTEXT.
- Không dùng khối mã.
- Không dùng HTML thô.
- Không link tuyệt đối.
- Nhắc 2-4 bài trong kỳ thi bằng Markdown link nếu có.
- Nếu ARTICLE_MODE là PER_PROBLEM_ANALYSIS,
  phải viết đúng một đoạn cho từng bài công khai trong PROBLEMS_JSON, theo thứ tự
  kỳ thi. Mỗi đoạn bắt đầu bằng Markdown link của bài đó và có 1-2 câu: đề yêu cầu
  gì, sau đó mới nêu hướng suy nghĩ ban đầu. Không phân tích lại một bài ở đoạn khác.
  Ở câu kết, chỉ gọi tên bài, không lặp lại Markdown link.
- Chỉ khẳng định kỹ thuật/cách cài đặt khi nó được SOURCE_CONTEXT hỗ trợ trực tiếp
  bằng đề bài, EDITORIAL, hoặc VERIFIED_REFERENCE_SOLUTION. Nếu chưa có nguồn đó,
  mô tả yêu cầu của đề và câu hỏi cần làm rõ thay vì đoán lời giải.
- Với bài có cả EDITORIAL và VERIFIED_REFERENCE_SOLUTION rỗng, không được nêu tên
  thuật toán, cấu trúc dữ liệu, biến đổi đại số, hay độ phức tạp như một hướng giải.
  Chỉ tóm tắt đúng yêu cầu/constraint của đề và điều người đọc cần quan sát khi tự làm.
- Không bịa ví dụ, dữ liệu, hoặc kỹ thuật không có trong SOURCE_CONTEXT.
- Không giải trọn kỳ thi.
- Dùng ví dụ/tình huống cụ thể từ kỳ thi trước khi nói thuật ngữ.
- Có một câu chuyển tự nhiên: nên thử bài nào trước, hoặc bài nào giúp mở khóa ý tưởng nào.
- Nếu kỳ thi cơ bản, tránh từ nặng như điều luôn đúng, trạng thái, tối ưu hóa. Hãy nói bằng thao tác cụ thể: duyệt chỉ số nào, so sánh biến nào, cập nhật mảng nào.
- Không dùng câu mở kiểu quảng cáo hoặc câu đệm như “không gian nhẹ nhàng”, “khởi động tay chân”, “sa đà”. Vào thẳng bài đầu tiên nên thử và lý do.
- Ưu tiên tiếng Việt trong tiêu đề và câu văn. Thuật ngữ quen thuộc như DP, DFS, code, test, input, output, contest, editorial dùng được nếu tự nhiên; nếu dùng tên khó như Fenwick, giải thích ngay bằng tiếng Việt.
- Có thể dùng “code” hoặc “mã” theo ngữ cảnh; không ép thay một từ bằng từ kia.
- Có thể dùng `stack`, `queue`, và `deque` hoặc tên tiếng Việt tương ứng theo ngữ cảnh; không ép dịch thuật ngữ quen thuộc.

Kết bài:
- Không viết châm ngôn/lời khuyên chung.
- Kết bằng một gợi ý cụ thể: nên thử bài nào trước hoặc nên đọc đề theo thứ tự nào.

Chỉ trả về Markdown cuối cùng."""

REVIEW_SYSTEM_PROMPT = r"""Bạn là người duyệt và soát lỗi cuối cùng cho một bài viết LQDOJ.

Đọc SOURCE_CONTEXT và DRAFT, rồi chỉ trả JSON hợp lệ. Không viết lại bài.

Kiểm tra bốn việc:
1. Fact-check: mọi tên, link, số liệu, mô tả đề, và khẳng định thuật toán có được
   SOURCE_CONTEXT hỗ trợ không? Đề bài chỉ hỗ trợ việc mô tả yêu cầu và constraint,
   không tự hỗ trợ việc khẳng định thuật toán/cấu trúc dữ liệu/độ phức tạp. Một kỹ
   thuật chỉ được nêu như kết luận khi EDITORIAL hoặc VERIFIED_REFERENCE_SOLUTION
   trong SOURCE_CONTEXT hỗ trợ nó. Đừng chấp nhận một kỹ thuật chỉ vì nghe hợp lý.
2. Proofread: có câu sai nghĩa, tự mâu thuẫn, hoặc khó hiểu rõ ràng không?
3. Flow: các đoạn có đi theo một mạch tự nhiên không? Chặn khi bị lặp ý/bài, bỏ sót
   phần mà ADMINISTRATOR_GUIDANCE yêu cầu, hoặc chuyển ý sai đến mức khó theo dõi.
   Nếu được yêu cầu phân tích từng bài, ngay sau phần tóm tắt phải là đúng một đoạn
   cho mỗi bài theo thứ tự PROBLEMS_JSON; không có đoạn phân tích lẻ chen vào trước,
   không lặp bài, và không nhắc lại toàn bộ link ở phần kết.
4. Request: bài có thực sự đáp ứng ADMINISTRATOR_GUIDANCE không?

Đặt publishable=false chỉ khi có lỗi rõ ràng trong bốn việc trên. Đừng từ chối chỉ vì
văn phong cá nhân, độ dài, một câu hơi gượng, hoặc vì bài chưa đạt mức biên tập lý tưởng.
Nếu không duyệt, feedback nêu 1-3 lỗi cụ thể và cách sửa ngắn gọn. Nếu duyệt, feedback
để trống.

Trả về đúng JSON:
{"publishable": true/false, "score": 1-10, "feedback": ""}

Chỉ trả JSON."""

PROBLEM_SELECTION_SYSTEM_PROMPT = r"""Bạn là biên tập viên chọn bài lập trình công khai cho chuyên mục LQDOJ.

Nhiệm vụ: dùng công cụ để tự tìm và đọc đề, rồi trả JSON hợp lệ.
- Bắt đầu bằng search_public_problems để tìm ứng viên. Có thể gọi lại với truy vấn khác nếu kết quả chưa phù hợp.
- Trước khi tìm, tách EXAMPLE_DIRECTION/PURPOSE thành quan hệ cụ thể cần luyện:
  trạng thái hoặc dữ liệu đầu vào, các lựa chọn trước đó, đại lượng phải lấy tốt nhất,
  và thao tác cấu trúc dữ liệu. Giữ đủ quan hệ này trong truy vấn, không tìm bằng một
  nhãn đơn lẻ như "segment tree" hoặc "DP".
- Bạn có công cụ get_problem_statement(code) để đọc đề bài đầy đủ.
- Sau khi đã đọc đề, có thể gọi get_ac_solution(code) để kiểm tra cơ chế chính
  bằng một lời giải AC tham chiếu đã được xác minh.
- Sau khi đã đọc đề, có thể gọi get_editorial(code) để tham khảo editorial đã công khai.
- Bắt buộc gọi get_problem_statement cho mọi mã bài bạn định chọn trước khi chọn.
- Chọn đúng số lượng và cơ cấu độ khó được yêu cầu, ưu tiên bài có đề rõ ràng,
  phù hợp với cộng đồng, và có một ý tưởng đáng để viết bài giới thiệu.
- Không chọn bài chỉ vì cùng nhãn kỹ thuật hoặc điểm số.
- PURPOSE và EXAMPLE_DIRECTION nêu ý kết hợp cần luyện. Một bài chỉ khớp một từ
  khóa hay một thành phần của ý đó không đủ phù hợp.
- Khi MINIMUM_SELECTIONS lớn hơn 1, dùng ít nhất hai truy vấn semantic khác nhau
  trước khi kết luận không đủ bài. Mỗi truy vấn phải diễn đạt cùng quan hệ cốt lõi
  theo một cách khác, không chỉ thay một nhãn kỹ thuật.
- Chỉ dùng mã do search_public_problems trả về.

Trả về đúng JSON:
{"codes": ["ma-bai-1", "ma-bai-2"], "evidence": {"ma-bai-1": "bước chuyển/truy vấn cụ thể đã đọc trong đề"}}

`evidence` phải nêu rõ vì sao đề bài luyện đúng quan hệ trong PURPOSE, không chỉ lặp
lại tên nhãn kỹ thuật. Nếu không viết được evidence chính xác từ đề đã đọc, không chọn bài.

Chỉ trả JSON."""

CONTEST_SELECTION_SYSTEM_PROMPT = r"""Bạn chọn một kỳ thi để viết bài gợi ý đọc trên LQDOJ.

Nhiệm vụ: dùng công cụ để tự tìm và đọc kỳ thi, rồi trả JSON hợp lệ.
- Bắt đầu bằng search_public_contests để tìm ứng viên. Có thể gọi lại nếu cần.
- Bạn có công cụ get_contest_details(key) để đọc mô tả kỳ thi và đề các bài công khai.
- Bắt buộc gọi get_contest_details cho kỳ thi bạn định chọn trước khi chọn.
- Chọn kỳ thi có ít nhất hai bài công khai, có chất liệu cụ thể để gợi ý người đọc bắt đầu,
  và phù hợp với cộng đồng.
- Chỉ dùng key do search_public_contests trả về.

Trả về đúng JSON:
{"key": "contest-key"}

Chỉ trả JSON."""

AGENT_TOOL_PROTOCOL = r"""
Bạn đang làm việc qua một vòng lặp công cụ. Khi cần dùng công cụ, chỉ trả đúng JSON:
{"tool_call": {"name": "ten_cong_cu", "arguments": {}}}
Sau đó bạn sẽ nhận TOOL_RESULT và tiếp tục. Chỉ khi đã xong mới trả JSON kết quả cuối
theo định dạng được yêu cầu. Không đặt tool_call và kết quả cuối trong cùng một phản hồi.
"""


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
class PracticeProblem:
    code: str
    name: str
    url: str
    points: float
    types: list
    score: float = 0.0


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
            default="Gemini-3.7-Flash",
            help="Poe bot name to use for writing",
        )
        parser.add_argument(
            "--max-attempts",
            type=int,
            default=10,
            help="Maximum LLM write/rewrite attempts per post, capped at 5",
        )
        parser.add_argument(
            "--skip-review",
            action="store_true",
            help="Skip the LLM reviewer pass",
        )
        parser.add_argument(
            "--review-threshold",
            type=int,
            default=6,
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
        self.max_llm_attempts = min(max(1, options["max_attempts"]), 5)
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
            self._mixed_post_plan(options["count"], rng, org)
            if post_type == "mixed"
            else []
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
                    None, difficulties, used_codes, org
                )
                chosen = self._choose_problem_candidates(
                    service, rng, candidates, difficulties
                )
            for candidate in chosen:
                generated.append(self._generate_problem_post(service, candidate))

        if post_type == "contest" or "contest" in mixed_plan:
            contest = self._select_contest(
                service, rng, options["contest"], history, org
            )
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

    def _mixed_post_plan(self, count, rng, org=None):
        if count <= 0:
            return []
        if self._is_contest_discussion_org(org):
            return ["contest"] * count
        base = ["problem", "topic", "contest"]
        if count == 1:
            return rng.choices(base, weights=(45, 45, 10), k=1)
        if count <= len(base):
            return rng.sample(base, count)
        plan = list(base)
        plan.extend(rng.choice(("problem", "topic")) for _ in range(count - len(base)))
        rng.shuffle(plan)
        return plan

    def _is_contest_discussion_org(self, org):
        text = self._organization_text(org).lower()
        slug = getattr(org, "slug", "").lower()
        markers = (
            "thảo luận kỳ thi",
            "thao luan ky thi",
            "thao-luan-ky-thi",
            "contest discussion",
        )
        return any(marker in text or marker in slug for marker in markers)

    def _collect_problem_candidates(self, service, difficulties, used_codes, org):
        candidates = []
        seen = set(used_codes)
        for difficulty in set(difficulties):
            candidates.extend(
                self._semantic_problem_candidates(service, difficulty, seen, org)
            )
            candidates.extend(self._activity_problem_candidates(difficulty, seen, org))
            candidates.extend(
                self._recent_contest_problem_candidates(difficulty, seen, org)
            )
        return candidates

    def _semantic_problem_candidates(self, service, difficulty, seen, org):
        if not getattr(settings, "USE_ML", False):
            return []
        results = []
        low, high = self._difficulty_range(difficulty, org)
        for query in self._queries_for_org(difficulty):
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
            self._base_public_contest_queryset()
            .filter(end_time__lte=timezone.now(), end_time__gte=cutoff)
            .values_list("id", flat=True)[:10]
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
            self._base_public_problem_queryset()
            .filter(
                description__gt="",
                user_count__gte=10,
            )
            .select_related("group")
            .prefetch_related("types")
        )

    def _base_public_problem_queryset(self):
        return Problem.objects.filter(
            is_public=True,
            is_organization_private=False,
        )

    def _queries_for_org(self, difficulty):
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
            statement=statement,
            source=source,
            semantic_score=float(kwargs.get("semantic_score", 0.0)),
            recent_users=int(kwargs.get("recent_users", 0)),
            recent_submissions=int(kwargs.get("recent_submissions", 0)),
            contest_name=kwargs.get("contest_name", ""),
        )

    def _fixed_problem_candidate(self, code, difficulty, org):
        try:
            problem = (
                self._base_public_problem_queryset()
                .filter(description__gt="")
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

    def _choose_problem_candidates(self, service, rng, candidates, difficulties):
        fallback = self._choose_problem_candidates_fallback(
            rng, candidates, difficulties
        )
        if not service:
            return fallback

        selected = self._select_public_problems_with_agent(
            service=service,
            org=getattr(self, "target_org", None),
            expected_difficulties=difficulties,
            max_count=len(difficulties),
            minimum_count=len(difficulties),
            purpose=(
                "Chọn bài cho chuyên mục Bài gợi ý. Mỗi bài cần có một ý tưởng "
                "cụ thể đáng để giải thích, không chỉ phổ biến."
            ),
        )
        return selected if len(selected) == len(difficulties) else fallback

    def _select_public_problems_with_agent(
        self, service, org, expected_difficulties, max_count, minimum_count, purpose
    ):
        expected_difficulties = list(expected_difficulties or ())
        prompt = f"""COMMUNITY_CONTEXT:
{self._organization_text(org) or "general"}
AUDIENCE_LEVEL: {self._audience_level(org)}
REQUIRED_DIFFICULTIES: {json.dumps(expected_difficulties, ensure_ascii=False)}
MAXIMUM_SELECTIONS: {max_count}
MINIMUM_SELECTIONS: {minimum_count}
PURPOSE: {purpose}
"""
        read_codes = set()
        try:
            response = self._call_agent_with_tools(
                service=service,
                prompt=prompt,
                system_prompt=PROBLEM_SELECTION_SYSTEM_PROMPT,
                tools=self._public_problem_tool_definitions(),
                tool_executables=self._public_problem_tool_executables(
                    org, expected_difficulties, read_codes
                ),
            )
        except Exception:
            return []

        result = self._parse_review_response(response or "")
        selected_codes = result.get("codes") if isinstance(result, dict) else None
        if not isinstance(selected_codes, list):
            return []
        evidence = result.get("evidence", {})
        if not isinstance(evidence, dict):
            return []

        selected_codes = [str(code).strip() for code in selected_codes]
        selected_codes = list(dict.fromkeys(code for code in selected_codes if code))
        selected_codes = [
            code
            for code in selected_codes
            if code in read_codes and len(str(evidence.get(code, "")).strip()) >= 24
        ]
        if not selected_codes:
            return []
        if len(selected_codes) < minimum_count:
            return []

        selected_map = self._public_problem_candidate_map(
            selected_codes, org, expected_difficulties
        )
        selected_by_difficulty = {}
        for code in selected_codes:
            candidate = selected_map.get(code)
            if candidate:
                selected_by_difficulty.setdefault(candidate.difficulty, []).append(
                    candidate
                )

        selected = []
        used_codes = set()
        for difficulty in expected_difficulties:
            options = selected_by_difficulty.get(difficulty, [])
            candidate = next(
                (item for item in options if item.code not in used_codes), None
            )
            if candidate is None:
                return []
            selected.append(candidate)
            used_codes.add(candidate.code)

        if expected_difficulties:
            return selected[:max_count]
        resolved = [
            selected_map[code] for code in selected_codes if code in selected_map
        ][:max_count]
        return resolved if len(resolved) >= minimum_count else []

    def _public_problem_candidate_map(self, codes, org, expected_difficulties):
        problems = (
            self._base_problem_queryset()
            .filter(code__in=codes)
            .select_related("group")
            .prefetch_related("types")
        )
        candidates = {}
        for problem in problems:
            if not self._audience_problem_ok(problem, org):
                continue
            difficulty = (
                self._problem_difficulty(problem, org, expected_difficulties)
                if expected_difficulties
                else "practice"
            )
            if not difficulty:
                continue
            candidate = self._candidate_from_problem(
                problem, difficulty, "agentic_search", org=org
            )
            if candidate:
                candidates[candidate.code] = candidate
        return candidates

    def _problem_difficulty(self, problem, org, expected_difficulties):
        difficulties = expected_difficulties or DIFFICULTY_RANGES
        for difficulty in difficulties:
            low, high = self._difficulty_range(difficulty, org)
            if low <= problem.points <= high:
                return difficulty
        return None

    def _public_problem_tool_definitions(self):
        return [
            fp.ToolDefinition(
                type="function",
                function={
                    "name": "search_public_problems",
                    "description": (
                        "Semantic-search public, non-private LQDOJ problems that fit "
                        "the requested audience and difficulty buckets."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "A semantic search query in Vietnamese or English.",
                            },
                            "difficulty": {
                                "type": "string",
                                "description": "One required difficulty bucket, if applicable.",
                            },
                        },
                        "required": ["query"],
                    },
                },
            ),
            *self._problem_statement_tool_definitions(),
            fp.ToolDefinition(
                type="function",
                function={
                    "name": "get_ac_solution",
                    "description": (
                        "Get a verified reference accepted solution for a public "
                        "problem after reading its statement. Never returns user "
                        "submission source code."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "code": {
                                "type": "string",
                                "description": (
                                    "A problem code already read with "
                                    "get_problem_statement."
                                ),
                            }
                        },
                        "required": ["code"],
                    },
                },
            ),
            fp.ToolDefinition(
                type="function",
                function={
                    "name": "get_editorial",
                    "description": (
                        "Get the published public editorial for a public problem "
                        "after reading its statement."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "code": {
                                "type": "string",
                                "description": (
                                    "A problem code already read with "
                                    "get_problem_statement."
                                ),
                            }
                        },
                        "required": ["code"],
                    },
                },
            ),
        ]

    def _public_problem_tool_executables(self, org, expected_difficulties, read_codes):
        allowed_difficulties = set(expected_difficulties or DIFFICULTY_RANGES)

        @sync_to_async
        def search_public_problems(query, difficulty=None):
            close_old_connections()
            difficulty = str(difficulty or "").strip()
            difficulties = (
                [difficulty]
                if difficulty in allowed_difficulties
                else allowed_difficulties
            )
            try:
                rows = search_problems(str(query).strip(), limit=20)
            except SemanticSearchUnavailable:
                return "Semantic search is unavailable. Try another approach later."

            codes = [row["code"] for row in rows]
            score_map = {row["code"]: float(row.get("score", 0.0)) for row in rows}
            results = []
            for problem in self._base_problem_queryset().filter(code__in=codes):
                if not self._audience_problem_ok(problem, org):
                    continue
                matched_difficulty = (
                    self._problem_difficulty(problem, org, difficulties)
                    if expected_difficulties
                    else "practice"
                )
                if not matched_difficulty:
                    continue
                results.append(
                    {
                        "code": problem.code,
                        "name": problem.name,
                        "difficulty": matched_difficulty,
                        "points": problem.points,
                        "types": [item.full_name for item in problem.types.all()],
                        "semantic_score": score_map.get(problem.code, 0.0),
                    }
                )
            return json.dumps(results[:12], ensure_ascii=False)

        @sync_to_async
        def get_problem_statement(code):
            close_old_connections()
            code = str(code).strip()
            try:
                problem = self._base_problem_queryset().get(code=code)
            except Problem.DoesNotExist:
                return "Unknown or ineligible problem code."
            if not self._audience_problem_ok(problem, org) or (
                expected_difficulties
                and not self._problem_difficulty(problem, org, allowed_difficulties)
            ):
                return "This problem is not eligible for this selection."
            read_codes.add(code)
            statement = self._clean_statement(problem.description)
            return self._statement_tool_result(problem, statement)

        @sync_to_async
        def get_ac_solution(code):
            close_old_connections()
            code = str(code).strip()
            if code not in read_codes:
                return "Read the public problem statement before requesting a solution."
            solution = (
                ProblemSolutionCode.objects.filter(
                    problem__code=code,
                    expected_result="AC",
                    last_submission__result="AC",
                )
                .select_related("language")
                .order_by("order", "id")
                .first()
            )
            if not solution:
                return "No verified reference AC solution is available."
            source = solution.source_code
            if len(source) > 12000:
                source = source[:12000] + "\n\n... (truncated)"
            return (
                f"Verified reference AC solution for {code} "
                f"(language={solution.language.key}):\n{source}"
            )

        @sync_to_async
        def get_editorial(code):
            close_old_connections()
            code = str(code).strip()
            if code not in read_codes:
                return (
                    "Read the public problem statement before requesting an editorial."
                )
            editorial = (
                Solution.objects.filter(
                    problem__code=code,
                    is_public=True,
                    publish_on__lte=timezone.now(),
                )
                .only("content")
                .first()
            )
            if not editorial:
                return "No published public editorial is available."
            content = editorial.content
            if len(content) > 16000:
                content = content[:16000] + "\n\n... (truncated)"
            return f"Published editorial for {code}:\n{content}"

        search_public_problems.__name__ = "search_public_problems"
        get_problem_statement.__name__ = "get_problem_statement"
        get_ac_solution.__name__ = "get_ac_solution"
        get_editorial.__name__ = "get_editorial"
        return [
            search_public_problems,
            get_problem_statement,
            get_ac_solution,
            get_editorial,
        ]

    def _statement_tool_result(self, problem, statement):
        if len(statement) > 8000:
            statement = statement[:8000] + "\n\n... (truncated)"
        return f"Problem {problem.code}: {problem.name}\n\n{statement}"

    def _call_agent_with_tools(
        self, service, prompt, system_prompt, tools, tool_executables
    ):
        executable_map = {tool.__name__: tool for tool in tool_executables}
        tool_names = ", ".join(executable_map)
        conversation_messages = []
        tool_system_prompt = (
            f"{system_prompt}\n{AGENT_TOOL_PROTOCOL}\n"
            f"Công cụ khả dụng: {tool_names}."
        )
        current_prompt = prompt

        for _ in range(8):
            response = service.call_llm_with_history(
                conversation_messages=conversation_messages,
                current_prompt=current_prompt,
                system_prompt=tool_system_prompt,
            )
            if not response:
                return None
            result = self._parse_review_response(response)
            tool_call = result.get("tool_call") if isinstance(result, dict) else None
            if not isinstance(tool_call, dict):
                return response

            name = str(tool_call.get("name", "")).strip()
            arguments = tool_call.get("arguments", {})
            executable = executable_map.get(name)
            if not executable or not isinstance(arguments, dict):
                return None
            try:
                tool_result = async_to_sync(executable)(**arguments)
            except (TypeError, ValueError):
                return None

            conversation_messages.extend(
                (
                    {"role": "assistant", "content": response},
                    {
                        "role": "user",
                        "content": f"TOOL_RESULT {name}:\n{tool_result}",
                    },
                )
            )
            current_prompt = (
                "Tiếp tục dùng công cụ nếu cần, hoặc trả JSON kết quả cuối."
            )
        return None

    def _choose_problem_candidates_fallback(self, rng, candidates, difficulties):
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
        practice_problems = self._problem_practice_problems(service, candidate)
        prompt = self._problem_prompt(candidate, practice_problems)
        content = self._call_with_validation(
            service,
            prompt,
            PROBLEM_SYSTEM_PROMPT,
            lambda body: self._validate_problem_post(
                body, candidate, practice_problems
            ),
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
        return self.generate_topic_post_with_feedback(service, topic, org)

    def _configure_llm_generation(self):
        self.max_llm_attempts = getattr(
            self,
            "max_llm_attempts",
            min(max(1, int(getattr(settings, "MAGAZINE_MAX_ATTEMPTS", 12))), 5),
        )
        self.enable_llm_review = getattr(self, "enable_llm_review", True)
        self.review_threshold = getattr(
            self,
            "review_threshold",
            min(max(int(getattr(settings, "MAGAZINE_REVIEW_THRESHOLD", 6)), 1), 10),
        )
        self.candidate_drafts = getattr(
            self,
            "candidate_drafts",
            max(1, int(getattr(settings, "MAGAZINE_CANDIDATE_DRAFTS", 1))),
        )

    def generate_topic_post_with_feedback(
        self,
        service,
        topic,
        org,
        feedback="",
        current_draft=None,
        progress_callback=None,
    ):
        """Generate a topic post for cron or an administrator-guided composer."""
        self.target_org = org
        self._configure_llm_generation()
        if progress_callback:
            progress_callback(_("Preparing the community writing guide"), 1, 5)
        guide = self._topic_example_guide(topic, org)
        if progress_callback:
            progress_callback(_("Selecting relevant practice problems"), 2, 5)
        practice_problems = self._topic_practice_problems(service, topic, org, guide)
        prompt = self._topic_prompt(topic, org, guide, practice_problems)
        if feedback:
            prompt += f"""

ADMINISTRATOR_GUIDANCE:
{feedback}
"""
        if current_draft:
            prompt += f"""

CURRENT_DRAFT:
TITLE: {current_draft.get('title', topic)}
SUMMARY: {current_draft.get('summary', '')}
CONTENT:
{current_draft.get('content', '')}

Rewrite the current draft from the ground up where necessary to follow the administrator guidance. Preserve correct, relevant details, but the required magazine format, example direction, practice recommendations, and quality rules remain authoritative.
"""
        content = self._call_with_validation(
            service,
            prompt,
            TOPIC_SYSTEM_PROMPT,
            lambda body: self._validate_topic_post(
                body, topic, guide, practice_problems
            ),
            progress_callback=progress_callback,
        )
        return GeneratedPost(
            title=topic[:100],
            summary=f"Chủ đề chuyên mục: {topic}",
            content=content,
            topic=topic,
        )

    def _topic_prompt(self, topic, org, guide, practice_problems=None):
        practice_json = json.dumps(
            [problem.__dict__ for problem in practice_problems or ()],
            ensure_ascii=False,
            indent=2,
        )
        return f"""TOPIC_TITLE: {topic}
AUDIENCE:
{self._audience_prompt_text(None)}
EXAMPLE_DIRECTION:
{guide.instruction}
PRACTICE_PROBLEMS_JSON:
{practice_json}

Viết một bài chuyên mục ngắn theo thứ tự:
1. Bắt đầu bằng **Tóm tắt:** trong một câu ngắn.
2. Mở đoạn thân bài đầu tiên bằng một tình huống cụ thể.
3. Cho ngay ví dụ nhỏ trong EXAMPLE_DIRECTION: số/xâu/dãy nếu là thuật toán, hoặc một tình huống thật nếu là học tập/cộng đồng.
4. Chỉ ra điều cần chú ý: dữ liệu cần lưu, câu hỏi cần hỏi, lỗi cần ghi lại, hoặc chi tiết cần chọn.
5. Nói điều đó thay đổi hoặc giúp quyết định bước tiếp theo như thế nào.
6. Cho một mẩu làm việc thật nhỏ: một bộ kiểm thử, một dòng ghi chú mẫu, một công thức, một biến/điều luôn đúng, hoặc danh sách kiểm tra 2-3 bước.
7. Nêu bước thực hiện cụ thể. Chỉ dùng mẩu mã trong dấu `...` nếu thật sự đang nói về mã.
8. Kết bằng một thao tác cụ thể người đọc có thể thử, không kết luận chung chung.

Nếu PRACTICE_PROBLEMS_JSON không rỗng, thêm mục **Bài áp dụng:** gần cuối bài với 2-3 link từ đúng JSON đó. Sau mỗi link, nói ngắn bài đó luyện được phần nào của ý tưởng. Không tự bịa link bài.

Không viết dòng **Chủ đề:** TOPIC_TITLE trong nội dung. Tiêu đề đã nằm ngoài bài.

Bắt buộc dùng đúng EXAMPLE_DIRECTION làm ví dụ chính của bài. Không thay bằng ví dụ khác tiện tay hơn."""

    def _topic_practice_problems(self, service, topic, org, guide):
        if not self._topic_supports_practice_problems(topic, org, guide):
            return []
        if not getattr(settings, "USE_ML", False):
            return []
        candidates = []
        for _ in range(2):
            candidates = self._select_public_problems_with_agent(
                service=service,
                org=org,
                expected_difficulties=(),
                max_count=3,
                minimum_count=2,
                purpose=f"""Chọn 2-3 bài áp dụng cho bài chia sẻ kiến thức.
TOPIC_TITLE: {topic}
EXAMPLE_DIRECTION: {guide.instruction}
Chỉ chọn khi đề bài thực sự cho người đọc thực hành ý chính của chủ đề.
EXAMPLE_DIRECTION là yêu cầu chính xác: bài được chọn phải thực hành đủ mối liên hệ
được mô tả ở đó, không chỉ một thuật ngữ hoặc một thao tác riêng lẻ.""",
            )
            if len(candidates) >= 2:
                break
        return [
            PracticeProblem(
                code=candidate.code,
                name=candidate.name,
                url=candidate.url,
                points=candidate.points,
                types=candidate.types,
            )
            for candidate in candidates
        ]

    def _problem_statement_tool_definitions(self):
        return [
            fp.ToolDefinition(
                type="function",
                function={
                    "name": "get_problem_statement",
                    "description": (
                        "Get the full statement text for one candidate problem code."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "code": {
                                "type": "string",
                                "description": (
                                    "A problem code returned by search_public_problems."
                                ),
                            }
                        },
                        "required": ["code"],
                    },
                },
            )
        ]

    def _topic_supports_practice_problems(self, topic, org, guide):
        if self._is_knowledge_share_org(org):
            return True
        org_text = self._organization_text(org).lower()
        slug = getattr(org, "slug", "").lower()
        excluded_markers = (
            "off-topic",
            "tán gẫu",
            "tan gau",
            "hỏi đáp",
            "hoi dap",
            "thắc mắc",
            "thac mac",
        )
        if self._is_contest_discussion_org(org) or any(
            marker in org_text or marker in slug for marker in excluded_markers
        ):
            return False
        return True

    def _is_knowledge_share_org(self, org):
        text = self._organization_text(org).lower()
        slug = getattr(org, "slug", "").lower()
        markers = (
            "tài liệu học tập",
            "tai lieu hoc tap",
            "tai-lieu-hoc-tap",
            "chia sẻ kiến thức",
            "chia se kien thuc",
            "knowledge share",
        )
        return any(marker in text or marker in slug for marker in markers)

    def _topic_example_guide(self, topic, org):
        level = self._audience_level(org)
        specific_guide = self._specific_topic_example_guide(topic, level)
        if specific_guide:
            return specific_guide
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

    def _specific_topic_example_guide(self, topic, level):
        lower = topic.lower()
        is_dp_topic = "dp" in lower or "quy hoạch động" in lower
        is_optimization_topic = "tối ưu" in lower or "cấu trúc dữ liệu" in lower
        if is_dp_topic and is_optimization_topic:
            if level == "middle":
                return TopicExampleGuide(
                    instruction=(
                        "Dùng ví dụ nhảy trên dãy điểm: tại vị trí `i`, chỉ được nhảy từ "
                        "một trong 3 vị trí trước đó, nên `dp[i]` cần lấy giá trị lớn nhất "
                        "trong một cửa sổ nhỏ. Trước hết thử duyệt 3 vị trí, rồi hỏi nếu cửa "
                        "sổ tăng lên 50 hoặc 100 vị trí thì cần lưu gì để lấy nhanh. "
                        "Chỉ nhắc deque ở phiên bản cửa sổ lớn."
                    ),
                    required_markers=("dp[i]", "3 vị trí", "lớn nhất", "cửa sổ"),
                    label="quy hoạch động cửa sổ nhỏ",
                )
            return TopicExampleGuide(
                instruction=(
                    "Dùng ví dụ tối ưu quy hoạch động: `dp[i]` cần lấy giá trị nhỏ nhất "
                    "trên các trạng thái `j` nằm trong một đoạn trước `i`. Thử cách duyệt "
                    "hết các `j`, rồi chuyển sang câu hỏi: cấu trúc nào giữ được giá trị "
                    "nhỏ nhất hiện tại khi cửa sổ trượt?"
                ),
                required_markers=("dp[i]", "nhỏ nhất", "đoạn", "cửa sổ"),
                label="tối ưu quy hoạch động bằng cửa sổ",
            )
        return None

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
                required_markers=("deque", "hàng đợi hai đầu", "0", "1", "trọng số"),
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

    def _validate_topic_post(self, body, topic, guide, practice_problems=None):
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
        if practice_problems and not any(
            problem.url in body for problem in practice_problems
        ):
            errors.append(
                "Bài topic cần gợi ý ít nhất một bài áp dụng từ PRACTICE_PROBLEMS_JSON"
            )
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

    def _problem_practice_problems(self, service, candidate):
        if not service or not getattr(settings, "USE_ML", False):
            return []
        selected = []
        for _ in range(2):
            selected = self._select_public_problems_with_agent(
                service=service,
                org=getattr(self, "target_org", None),
                expected_difficulties=(),
                max_count=3,
                minimum_count=2,
                purpose=f"""Chọn 2-3 bài tập tương tự cho bài gợi ý này.
FEATURED_PROBLEM_TITLE: {candidate.name}
FEATURED_PROBLEM_STATEMENT:
{candidate.statement[:4000]}
Chỉ chọn bài thực hành cùng ý tưởng chính. Không chọn bài chỉ trùng nhãn kỹ thuật,
và không chọn lại bài featured.""",
            )
            if len(selected) >= 2:
                break
        return [
            PracticeProblem(
                code=item.code,
                name=item.name,
                url=item.url,
                points=item.points,
                types=item.types,
            )
            for item in selected
            if item.code != candidate.code
        ]

    def _problem_prompt(self, candidate, practice_problems=None):
        practice_json = json.dumps(
            [problem.__dict__ for problem in practice_problems or ()],
            ensure_ascii=False,
            indent=2,
        )
        return f"""PROBLEM_TITLE: {candidate.name}
PROBLEM_URL: {candidate.url}
DIFFICULTY_MODE: {candidate.difficulty}
SOURCE: {candidate.source}
POINTS: {candidate.points}
AC_RATE: {candidate.ac_rate}
TYPES: {', '.join(candidate.types)}
STATEMENT:
{candidate.statement[:1800]}
SIMILAR_PRACTICE_PROBLEMS_JSON:
{practice_json}

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
7. Nếu SIMILAR_PRACTICE_PROBLEMS_JSON không rỗng, thêm mục **Bài tập tương tự:** gần cuối bài với 2-3 link từ đúng JSON đó và nói ngắn mỗi bài luyện được phần nào của ý tưởng.
8. Dừng ở một thao tác cụ thể của bài, không kết luận chung chung."""

    def _audience_prompt_text(self, candidate):
        org = getattr(self, "target_org", None)
        if org:
            level = self._audience_level(org)
            level_hints = {
                "primary": "Ưu tiên câu ngắn, ví dụ thật cụ thể, rất ít thuật ngữ.",
                "middle": (
                    "Có thể có bài thử thách, nhưng phải đi từ ví dụ cụ thể trước. "
                    "Ưu tiên mảng, xâu, đếm, sắp xếp, mảng cộng dồn, queue/deque đơn giản; "
                    "tránh cây phân đoạn/Fenwick nếu không giải thích rất chậm."
                ),
                "high": (
                    "Có thể dùng thuật ngữ thuật toán, nhưng vẫn cần giải thích mạch suy nghĩ. "
                    "Nếu dùng cấu trúc dữ liệu khó, phải có ví dụ nhỏ và thao tác lưu/cập nhật rõ."
                ),
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
        if candidate is None:
            return (
                "Học sinh phổ thông trong cộng đồng được chọn. "
                "Điều chỉnh độ sâu theo nội dung bài viết."
            )
        if "tiểu học" in candidate.group.lower() or "bảng a" in candidate.group.lower():
            return "Học sinh tiểu học hoặc Tin học trẻ bảng A. Cần câu ngắn, ví dụ thật cụ thể, thuật ngữ rất ít."
        if candidate.difficulty in ("challenge", "stretch"):
            return "Học sinh đang luyện nghiêm túc. Có thể giải thích dài hơn, nhưng vẫn phải đi từ ví dụ cụ thể trước."
        return "Học sinh phổ thông trong cộng đồng được chọn. Giải thích vừa đủ, tránh quá nhiều thuật ngữ."

    def _validate_problem_post(self, body, candidate, practice_problems=None):
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
        if practice_problems and not any(
            problem.url in body for problem in practice_problems
        ):
            errors.append(
                "Bài gợi ý cần thêm ít nhất một bài từ SIMILAR_PRACTICE_PROBLEMS_JSON"
            )
        return errors

    def _select_contest(self, service, rng, key, history, org):
        queryset = self._base_public_contest_queryset()
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
            .annotate(
                problem_count=Count(
                    "contest_problems",
                    filter=Q(
                        contest_problems__problem__is_public=True,
                        contest_problems__problem__is_organization_private=False,
                    ),
                )
            )
            .filter(problem_count__gte=2)
            .order_by("-end_time")[:12]
        )
        contests = [
            contest for contest in contests if contest.key not in history.contest_keys
        ]
        if not contests:
            raise CommandError("No recent visible public contest found")
        fallback = rng.choice(contests[: min(6, len(contests))])
        if not service:
            return fallback

        searched_keys = set()
        read_keys = set()
        prompt = f"""COMMUNITY_CONTEXT:
{self._organization_text(org) or "general"}
AUDIENCE_LEVEL: {self._audience_level(org)}
PURPOSE: Chọn một kỳ thi công khai gần đây để viết bài gợi ý đọc. Kỳ thi cần có
ít nhất hai bài công khai và có vài bài đủ cụ thể để gợi ý người đọc bắt đầu.
"""
        try:
            response = self._call_agent_with_tools(
                service=service,
                prompt=prompt,
                system_prompt=CONTEST_SELECTION_SYSTEM_PROMPT,
                tools=self._public_contest_tool_definitions(),
                tool_executables=self._public_contest_tool_executables(
                    history, searched_keys, read_keys
                ),
            )
        except Exception:
            return fallback

        result = self._parse_review_response(response or "")
        selected_key = (
            str(result.get("key", "")).strip() if isinstance(result, dict) else ""
        )
        if (
            not selected_key
            or selected_key not in searched_keys
            or selected_key not in read_keys
        ):
            return fallback
        contest_map = {contest.key: contest for contest in contests}
        return contest_map.get(selected_key, fallback)

    def _public_contest_tool_definitions(self):
        return [
            fp.ToolDefinition(
                type="function",
                function={
                    "name": "search_public_contests",
                    "description": (
                        "List recent public LQDOJ contests with at least two public "
                        "problems. An optional query filters title and description."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "Optional Vietnamese or English keywords.",
                            }
                        },
                    },
                },
            ),
            fp.ToolDefinition(
                type="function",
                function={
                    "name": "get_contest_details",
                    "description": (
                        "Get a searched contest's description and full public problem "
                        "statements before deciding whether to select it."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "key": {
                                "type": "string",
                                "description": "A key returned by search_public_contests.",
                            }
                        },
                        "required": ["key"],
                    },
                },
            ),
        ]

    def _public_contest_tool_executables(self, history, searched_keys, read_keys):
        @sync_to_async
        def search_public_contests(query=""):
            close_old_connections()
            cutoff = timezone.now() - timedelta(days=45)
            contests = self._eligible_recent_contests(cutoff, history.contest_keys)
            query = str(query).strip()
            if query:
                normalized_query = query.lower()
                contests = [
                    contest
                    for contest in contests
                    if normalized_query in contest.name.lower()
                    or normalized_query
                    in self._clean_statement(contest.description).lower()
                ]
            results = []
            for contest in contests:
                searched_keys.add(contest.key)
                results.append(
                    {
                        "key": contest.key,
                        "name": contest.name,
                        "end_time": contest.end_time.isoformat(),
                        "problem_count": contest.problem_count,
                    }
                )
            return json.dumps(results[:12], ensure_ascii=False)

        @sync_to_async
        def get_contest_details(key):
            close_old_connections()
            key = str(key).strip()
            if key not in searched_keys or key in history.contest_keys:
                return "Unknown or ineligible contest key."
            try:
                contest = self._base_public_contest_queryset().get(key=key)
            except Contest.DoesNotExist:
                return "Unknown or ineligible contest key."
            rows = (
                ContestProblem.objects.filter(
                    contest=contest,
                    problem__isnull=False,
                    problem__is_public=True,
                    problem__is_organization_private=False,
                )
                .select_related("problem")
                .prefetch_related("problem__types")
                .order_by("order")[:8]
            )
            problems = []
            for row in rows:
                statement = self._clean_statement(row.problem.description)
                if len(statement) > 5000:
                    statement = statement[:5000] + "\n\n... (truncated)"
                problems.append(
                    {
                        "code": row.problem.code,
                        "name": row.problem.name,
                        "points": row.problem.points,
                        "types": [item.full_name for item in row.problem.types.all()],
                        "statement": statement,
                    }
                )
            if len(problems) < 2:
                return "Contest no longer has enough eligible public problems."
            read_keys.add(key)
            description = self._clean_statement(contest.description)[:3000]
            return json.dumps(
                {
                    "key": contest.key,
                    "name": contest.name,
                    "description": description,
                    "problems": problems,
                },
                ensure_ascii=False,
            )

        search_public_contests.__name__ = "search_public_contests"
        get_contest_details.__name__ = "get_contest_details"
        return [search_public_contests, get_contest_details]

    def _eligible_recent_contests(self, cutoff, excluded_keys=()):
        contests = (
            self._base_public_contest_queryset()
            .filter(end_time__lte=timezone.now(), end_time__gte=cutoff)
            .annotate(
                problem_count=Count(
                    "contest_problems",
                    filter=Q(
                        contest_problems__problem__is_public=True,
                        contest_problems__problem__is_organization_private=False,
                    ),
                )
            )
            .filter(problem_count__gte=2)
            .order_by("-end_time")[:12]
        )
        return [contest for contest in contests if contest.key not in excluded_keys]

    def _base_public_contest_queryset(self):
        return Contest.objects.filter(
            is_visible=True,
            is_private=False,
            is_organization_private=False,
            is_in_course=False,
        )

    def _generate_contest_post(
        self,
        service,
        contest,
        administrator_guidance="",
        per_problem_analysis=False,
    ):
        self._configure_llm_generation()
        rows = (
            ContestProblem.objects.filter(
                contest=contest,
                problem__isnull=False,
                problem__is_public=True,
                problem__is_organization_private=False,
            )
            .select_related("problem", "problem__group")
            .prefetch_related("problem__types")
            .order_by("order")[:8]
        )
        problem_codes = [row.problem.code for row in rows]
        editorials = {}
        for editorial in (
            Solution.objects.filter(
                problem__code__in=problem_codes,
                is_public=True,
                publish_on__lte=timezone.now(),
            )
            .order_by("problem__code", "-publish_on", "-id")
            .values("problem__code", "content")
        ):
            editorials.setdefault(
                editorial["problem__code"],
                self._clean_statement(editorial["content"])[:3000],
            )
        reference_solutions = {}
        for solution in (
            ProblemSolutionCode.objects.filter(
                problem__code__in=problem_codes,
                expected_result="AC",
                last_submission__result="AC",
            )
            .select_related("language", "problem")
            .order_by("problem__code", "order", "id")
        ):
            reference_solutions.setdefault(
                solution.problem.code,
                "language=%s\n%s"
                % (solution.language.key, solution.source_code[:6000]),
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
                    "statement": self._clean_statement(problem.description)[:5000],
                    "editorial": editorials.get(problem.code, ""),
                    "verified_reference_solution": reference_solutions.get(
                        problem.code, ""
                    ),
                }
            )
        prompt = self._contest_prompt(
            contest,
            problems,
            administrator_guidance=administrator_guidance,
            per_problem_analysis=per_problem_analysis,
        )
        content = self._call_with_validation(
            service,
            prompt,
            CONTEST_SYSTEM_PROMPT,
            lambda body: self._validate_contest_post(
                body, contest, problems, per_problem_analysis
            ),
        )
        return GeneratedPost(
            title=f"Gợi ý đọc kỳ thi: {contest.name}",
            summary=f"Gợi ý đọc kỳ thi: {contest.name}",
            content=content,
            contest=contest,
        )

    def _contest_prompt(
        self, contest, problems, administrator_guidance="", per_problem_analysis=False
    ):
        return f"""CONTEST_TITLE: {contest.name}
CONTEST_URL: /contest/{contest.key}
CONTEST_DESCRIPTION:
{self._clean_statement(contest.description)[:1000]}

ADMINISTRATOR_GUIDANCE:
{administrator_guidance or "Write a concise contest reading recommendation."}

ARTICLE_MODE: {"PER_PROBLEM_ANALYSIS" if per_problem_analysis else "READING_RECOMMENDATION"}

AUDIENCE:
{self._audience_prompt_text(None)}

PROBLEMS_JSON:
{json.dumps(problems, ensure_ascii=False, indent=2)}

Viết bài đánh giá kỳ thi cho cộng đồng trên."""

    def _validate_contest_post(
        self, body, contest, problems=None, per_problem_analysis=False
    ):
        errors = self._common_markdown_errors(body)
        if "```" in body:
            errors.append("Không dùng khối mã trong bài kỳ thi")
        first_line = f"**Kỳ thi:** [{contest.name}](/contest/{contest.key})"
        lines = body.strip().splitlines()
        if not lines or lines[0].strip() != first_line:
            errors.append(f"Dòng đầu phải đúng: {first_line}")
        if not self._has_summary_after_first_line(lines):
            errors.append("Dòng nội dung đầu sau link phải bắt đầu bằng **Tóm tắt:**")
        if per_problem_analysis:
            errors.extend(self._per_problem_analysis_errors(body, problems or ()))
        return errors

    def _per_problem_analysis_errors(self, body, problems):
        lines = body.strip().splitlines()
        summary_index = next(
            (
                index
                for index, line in enumerate(lines)
                if line.strip().startswith("**Tóm tắt:**")
            ),
            None,
        )
        if summary_index is None:
            return []
        first_content = next(
            (line.strip() for line in lines[summary_index + 1 :] if line.strip()),
            "",
        )
        urls = [problem["url"] for problem in problems]
        first_problem_link = (
            "[%s](%s)" % (problems[0]["title"], problems[0]["url"]) if problems else ""
        )
        if first_problem_link and not first_content.startswith(first_problem_link):
            return [
                "Ngay sau tóm tắt, bắt đầu từng đoạn bằng link của bài đầu tiên trong kỳ thi"
            ]
        positions = [body.find(url) for url in urls]
        if any(position < 0 for position in positions):
            return ["Bài phân tích từng bài phải có link của mọi bài công khai"]
        if positions != sorted(positions):
            return ["Các đoạn phân tích phải theo đúng thứ tự bài trong kỳ thi"]
        if any(body.count(url) != 1 for url in urls):
            return ["Mỗi bài công khai chỉ được link và phân tích đúng một lần"]
        return []

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
        errors.extend(self._wiki_style_learning_path_errors(body_after_summary))

        sentences = self._split_sentences(" ".join(paragraphs))
        long_sentences = [
            sentence for sentence in sentences if len(sentence.split()) > 44
        ]
        if long_sentences:
            errors.append("Có câu quá dài; mỗi câu nên dưới 44 từ")

        dense_sentences = [
            sentence
            for sentence in sentences
            if len(sentence.split()) > 32 and sentence.count(",") >= 4
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
            if len(sentence.split()) > 30 and hits >= 5:
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

    def _wiki_style_learning_path_errors(self, body):
        lower = body.lower()
        technical_markers = (
            "dp",
            "quy hoạch động",
            "dfs",
            "bfs",
            "fenwick",
            "segment tree",
            "cây phân đoạn",
            "cấu trúc dữ liệu",
            "tham lam",
            "chặt nhị phân",
            "đồ thị",
            "truy vấn",
        )
        if not any(marker in lower for marker in technical_markers):
            return []

        has_example = bool(re.search(r"`[^`]+`|\$[^$]+\$|\b\d+\b", body)) or any(
            marker in lower for marker in ("ví dụ", "chẳng hạn", "giả sử")
        )
        has_state_or_operation = any(
            marker in lower
            for marker in (
                "lưu",
                "theo dõi",
                "cập nhật",
                "truy vấn",
                "mỗi nút",
                "mỗi đoạn",
                "mỗi trạng thái",
                "gọi",
                "đặt",
            )
        )
        has_direct_comparison = any(
            marker in lower
            for marker in (
                "cách trực tiếp",
                "duyệt hết",
                "làm lại",
                "chậm",
                "không khả thi",
                "thay vì",
            )
        )
        if has_example and (has_state_or_operation or has_direct_comparison):
            return []

        return [
            "Bài có thuật ngữ kỹ thuật nhưng chưa đi theo mạch dễ đọc: ví dụ nhỏ, cách trực tiếp, rồi thứ cần lưu/thao tác chính"
        ]

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

    def _call_with_validation(
        self, service, prompt, system_prompt, validate, progress_callback=None
    ):
        feedback = ""
        approved = []
        last_body = None
        for attempt in range(min(self.max_llm_attempts, 5)):
            if progress_callback:
                progress_callback(
                    _("Writing draft %(attempt)s") % {"attempt": attempt + 1}, 3, 5
                )
            full_prompt = prompt
            if feedback:
                full_prompt += (
                    "\n\nLần trước chưa đạt vì các lỗi sau:\n"
                    f"{feedback}\n\n"
                    "Hãy viết lại từ đầu, sửa trực tiếp từng góp ý. "
                    "Không chỉ đổi câu chữ ở đoạn cuối; nếu reviewer nói ví dụ sai, "
                    "phải đổi ví dụ hoặc đổi kỹ thuật cho khớp. "
                    "Chỉ trả lại Markdown cuối cùng."
                )
            body = service.call_llm(full_prompt, system_prompt=system_prompt)
            if not body:
                feedback = "Không có phản hồi"
                continue
            body = body.strip() + "\n"
            last_body = body
            if progress_callback:
                progress_callback(_("Validating the draft"), 4, 5)
            errors = validate(body)
            if errors:
                feedback = "; ".join(errors)
                self.stdout.write(self.style.WARNING(f"validation failed: {feedback}"))
                continue

            if not getattr(self, "enable_llm_review", True):
                return body

            if progress_callback:
                progress_callback(_("Reviewing the draft"), 5, 5)
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

            feedback = (
                f"Reviewer chưa duyệt ở ngưỡng {self.review_threshold}/10: "
                f"{review['feedback']}"
            )
            self.stdout.write(self.style.WARNING(f"validation failed: {feedback}"))
        if approved:
            approved.sort(key=lambda item: item[0], reverse=True)
            return approved[0][1]
        if last_body:
            self.stdout.write(
                self.style.WARNING(
                    "Returning the last draft after five unsuccessful review attempts"
                )
            )
            return last_body
        raise CommandError(f"LLM output failed validation: {feedback}")

    def _review_body(self, service, source_context, body):
        if not getattr(self, "enable_llm_review", True):
            return {"passed": True, "score": 10, "feedback": ""}

        review_prompt = f"""SOURCE_CONTEXT:
{source_context[:60000]}

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
