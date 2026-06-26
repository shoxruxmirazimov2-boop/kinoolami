# helper patch file showing new handlers added to bot.py
# (actual changes applied directly in bot.py)

from aiogram.types import InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

@dp.callback_query(F.data == "vip_menu")
async def vip_menu_cb(callback: CallbackQuery):
    p1 = db_query("SELECT value FROM settings WHERE key = 'premium_price_1kun'", fetchone=True)[0]
    p7 = db_query("SELECT value FROM settings WHERE key = 'premium_price_1hafta'", fetchone=True)[0]
    p15 = db_query("SELECT value FROM settings WHERE key = 'premium_price_15kun'", fetchone=True)[0]
    p30 = db_query("SELECT value FROM settings WHERE key = 'premium_price_30kun'", fetchone=True)[0]
    def show_price(p):
        return ("Bepul" if (not p or p == '0') else f"{p} so'm")
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text=f"1 kun — {show_price(p1)}", callback_data="buy_premium|1kun"))
    builder.row(InlineKeyboardButton(text=f"1 hafta — {show_price(p7)}", callback_data="buy_premium|1hafta"))
    builder.row(InlineKeyboardButton(text=f"15 kun — {show_price(p15)}", callback_data="buy_premium|15kun"))
    builder.row(InlineKeyboardButton(text=f"30 kun — {show_price(p30)}", callback_data="buy_premium|30kun"))
    builder.row(InlineKeyboardButton(text="Orqaga", callback_data="vip_back"))
    await callback.message.edit_text("VIP paketlar:", reply_markup=builder.as_markup())
    await callback.answer()

@dp.callback_query(F.data == "vip_back")
async def vip_back_cb(callback: CallbackQuery):
    await check_sub_cb(callback)
    await callback.answer()