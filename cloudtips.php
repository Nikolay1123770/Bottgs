<?php

$input = file_get_contents("php://input");
$data = json_decode($input, true);

if (!$data || !isset($data["status"])) {
    http_response_code(400);
    echo "Invalid";
    exit;
}

$status = $data["status"];
$amount = $data["amount"] ?? 0;
$order_id = intval($data["payload"] ?? 0);

if ($status !== "PAID" || $order_id === 0) {
    echo "Ignored";
    exit;
}

/* ===== обновляем SQLite ===== */
$db = new SQLite3('/app/metro_shop.db');

$stmt = $db->prepare(
    "UPDATE orders SET status='paid' WHERE id=:id AND status='awaiting_screenshot'"
);
$stmt->bindValue(':id', $order_id, SQLITE3_INTEGER);
$stmt->execute();

/* ===== уведомляем в Telegram ===== */

$query = $db->query("
SELECT u.tg_id, p.name FROM orders o
JOIN users u ON o.user_id = u.id
JOIN products p ON o.product_id = p.id
WHERE o.id = $order_id
");

$row = $query->fetchArray(SQLITE3_ASSOC);

if ($row) {
    $tg_id = $row["tg_id"];
    $product = $row["name"];

    $token = "ТОКЕН_ТВОЕГО_БОТА";

    file_get_contents(
        "https://api.telegram.org/bot$token/sendMessage?chat_id=$tg_id&text=" .
        urlencode("Ваш платеж подтверждён автоматически 🎉\nЗаказ #$order_id — {$product}.")
    );
}

echo "OK";
?>
