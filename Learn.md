 2xx = thành công (200 = OK); 4xx = client (người gọi) sai cái gì đó; 5xx = server (bên bạn) hỏng.
200 — OK, thành công
400 — Bad Request: dữ liệu gửi lên sai/thiếu/méo (cái ?period=hourly sai ở dashboard của bạn trả 400 chính là đây)
401 — chưa xác thực (thiếu/sai key)
403 — biết anh là ai nhưng không đủ quyền
404 — không tìm thấy thứ được yêu cầu
429 — gọi quá nhiều, chậm lại
500 — server tự hỏng (bug bên bạn)