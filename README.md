# Bot Hatobox → Telegram, GitHub Actions

Bản miễn phí chạy theo lịch, không phải VPS. Mã nguồn và cấu hình được công khai.
Chỉ theo dõi Mercari qua Hatobox: điện thoại/iPad, laptop/MacBook, máy ảnh/máy chơi game, tai nghe/loa.
Theo lựa chọn hiện tại, báo tin mới kèm giá và link; chưa đánh giá rẻ hay áp giá trần.

## Thiết lập
1. Tạo tài khoản GitHub Free nếu chưa có. Tạo kho **Public** mới, ví dụ `hatobox-alerts`.
2. Đưa toàn bộ nội dung thư mục này vào gốc kho, gồm `.github/workflows/hatobox.yml`.
   Nếu upload trên web không hiện thư mục ẩn, tạo riêng file `.github/workflows/hatobox.yml` bằng Add file → Create new file và dán nội dung file tương ứng.
3. Tạo bot Telegram bằng @BotFather → /newbot. Không đưa token vào mã nguồn hoặc chat hỗ trợ.
4. Lấy chat ID trên máy của bạn: cài Python 3.10+, chạy `python setup_telegram.py`, nhập token khi được hỏi, mở bot gửi `/start` và chọn đúng cuộc trò chuyện của bạn. Script xác minh kết nối và gửi tin thử, lưu `.env` chỉ ở máy bạn. Không upload `.env`.
5. GitHub → Settings → Secrets and variables → Actions → New repository secret:
   - `TELEGRAM_BOT_TOKEN`: token của bot.
   - `TELEGRAM_CHAT_ID`: chat ID lấy ở bước 4.
6. Actions → Hatobox Telegram → Run workflow (nhánh mặc định). Lần đầu tạo mốc và không gửi các tin đã có. Kiểm tra log xanh, rồi chờ tin mới phù hợp.
   Khi kích hoạt thực tế vẫn cần xác minh Hatobox truy cập được từ runner GitHub và Telegram nhận được thông báo.

## Lịch, trạng thái và giới hạn
- Lịch mỗi 5 phút ở phút 2, 7, 12… (UTC). GitHub có thể chạy trễ hoặc bỏ lượt; quá trình cài/chạy cũng cần thời gian. Không bảo đảm báo ngay khi người bán đăng.
- Chỉ chạy trên kho Public bằng runner tiêu chuẩn. Workflow tự bỏ qua kho Private để tránh dùng hạn mức phút của kho riêng tư.
- Trạng thái chống trùng được lưu trong GitHub Actions cache, không lưu token/chat ID. Cache không phải lưu trữ bền vững: nếu bị xóa/thu hồi, bot tạo lại mốc và có thể bỏ lỡ tin trong khoảng gián đoạn.
- Cache và log của kho công khai có thể truy cập được; chỉ chứa trạng thái các tin công khai. Không thêm dữ liệu riêng tư vào config hoặc state. Giữ mức cache mặc định, không bật mở rộng trả phí.
- Các lượt chạy dùng chung khóa concurrency, không hủy lượt đang chạy. Một chu kỳ lâu có thể làm trễ lượt sau.
- Nếu Hatobox yêu cầu xác minh, bot lưu dấu tạm dừng. Kiểm tra nguyên nhân trước khi chạy thủ công với `resume=true`. Không có chức năng vượt xác minh.
- GitHub có thể tắt lịch sau 60 ngày kho công khai không hoạt động. Cần kiểm tra Actions định kỳ và bật lại nếu bị tắt. Không có cơ chế tự tạo commit giả để duy trì lịch.
- Nếu không gặp mốc cũ trong tối đa 3 trang mỗi nguồn, có thể bỏ sót tin. Thu hẹp nguồn hoặc tăng max_pages nếu cần.
- Kết nối mạng bị ngắt đúng lúc gửi Telegram có thể gây gửi lại một tin; chưa bảo đảm gửi đúng một lần tuyệt đối.
- Giá từ Hatobox là giá hiển thị, không bảo đảm đã gồm mọi phí nhập/mua hộ/vận chuyển. Lọc qua tiêu đề có thể sai; kiểm tra tình trạng món hàng trên link trước khi mua.

## Điều chỉnh và dừng
Sửa `config.json` để thay nguồn, từ khóa, loại trừ hoặc giá trần `max_jpy`/`max_vnd` của từng nhóm. `poll_seconds` chỉ áp dụng chế độ chạy liên tục; lịch GitHub nằm trong workflow.
Để dừng: Actions → Hatobox Telegram → menu → Disable workflow.

Nguồn chính thức:
- https://docs.github.com/en/billing/concepts/product-billing/github-actions
- https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows
- https://github.com/actions/cache
