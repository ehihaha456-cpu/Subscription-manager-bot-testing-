"""Focused clone-bot feature mixin; behavior preserved from services.bot_manager."""

from handlers.common.clone_context import *
from handlers.clone.admin import business_automation, group_manager
from handlers.clone.group_manager_runtime import group_manager_new_members, group_manager_chat_member_welcome, group_manager_message, group_manager_special_callback
from handlers.clone.group_manager_protection_runtime import group_manager_protection_message, anti_flood_message
from handlers.clone.forced_join_runtime import forced_join_request, forced_join_auto_approve, forced_join_info_callback, forced_join_message_editor, forced_join_editor_callback, forced_join_editor_text_input, forced_join_editor_media_input, forced_join_toggle_callback, connect_forced_join_command
from handlers.clone.business_official_runtime import handle_business_connection, handle_business_message, handle_deleted_business_messages
from telegram.ext import BusinessConnectionHandler, BusinessMessagesDeletedHandler, ChatJoinRequestHandler, ChatMemberHandler
from telegram.request import HTTPXRequest


class CloneRuntimeAppMixin:
    async def clone_error_handler(self, update, context):
        bot_id = context.application.bot_data.get("seller_bot_id")
        owner_id = context.application.bot_data.get("seller_owner_id")
        logger.error(
            "Unhandled clone bot update error bot_id=%s owner_id=%s",
            bot_id,
            owner_id,
            exc_info=(type(context.error), context.error, context.error.__traceback__),
        )

    async def business_automation_text_handler(self, update, context):
        handled = await group_manager.handle_text(self, update, context)
        if handled:
            raise ApplicationHandlerStop
        handled = await business_automation.handle_text(self, update, context)
        if handled:
            raise ApplicationHandlerStop

    async def business_automation_media_handler(self, update, context):
        handled = await group_manager.handle_media(self, update, context)
        if handled:
            raise ApplicationHandlerStop
        handled = await business_automation.handle_media(self, update, context)
        if handled:
            raise ApplicationHandlerStop


    async def connected_group_command_guard(self, update, context):
        message = update.effective_message
        user = update.effective_user
        chat = update.effective_chat
        if not message or not user or not chat or chat.type not in {"group", "supergroup"}:
            return

        command = (message.text or "").split()[0].split("@", 1)[0].lstrip("/").lower()
        if command not in {"start", "version", "admin", "help", "connectgroup", "connectsupport"}:
            return

        # /connectgroup and /connectsupport are silent for everyone except
        # Clone Bot seller/staff admins. Telegram group-admin status alone is not enough.
        if command in {"connectgroup", "connectsupport"}:
            if not await self.auth(update, context):
                raise ApplicationHandlerStop
            return

        # /start, /version, /admin and /help must stay completely silent inside
        # groups already connected to this Clone Bot (subscription/group-manager
        # groups or the connected Live Support group), even for bot admins.
        owner = self.owner(context)
        connected = False
        try:
            channels = await get_channels(owner)
            connected = any(int(item.get("chat_id", 0) or 0) == int(chat.id) for item in channels or [])
        except Exception:
            logger.exception("Failed to check connected groups for command guard owner=%s chat=%s", owner, chat.id)

        if not connected:
            try:
                support = await get_live_support_settings(owner)
                support_group_id = int(support.get("support_group_id") or 0) if support else 0
                connected = support_group_id == int(chat.id)
            except Exception:
                logger.exception("Failed to check support group for command guard owner=%s chat=%s", owner, chat.id)

        if connected:
            raise ApplicationHandlerStop

    def build_app(self,token,data_owner_id,seller_account_id,bot_id=None):
        request = HTTPXRequest(
            connection_pool_size=48,
            pool_timeout=5.0,
            connect_timeout=5.0,
            read_timeout=20.0,
            write_timeout=20.0,
        )
        protected_bot=ProtectedExtBot(
            token=token,
            # Content protection must exempt the real seller account, not the
            # clone data-scope ID (which can be the bot ID).
            owner_id=int(seller_account_id),
            request=request,
        )
        app=(
            Application.builder()
            .bot(protected_bot)
            .concurrent_updates(1)
            .build()
        )
        app.bot_data["seller_owner_id"]=int(data_owner_id)
        app.bot_data["data_owner_id"]=int(data_owner_id)
        app.bot_data["seller_account_id"]=int(seller_account_id)
        app.bot_data["seller_bot_id"]=int(bot_id or 0)
        app.add_error_handler(self.clone_error_handler)
        # Official Telegram Business updates must run before normal clone-bot
        # moderation/menu handlers.
        app.add_handler(BusinessConnectionHandler(handle_business_connection), group=-51)
        app.add_handler(MessageHandler(filters.UpdateType.BUSINESS_MESSAGE, handle_business_message), group=-50)
        app.add_handler(BusinessMessagesDeletedHandler(handle_deleted_business_messages), group=-49)
        # Anti-flood MUST run before command/deletion guards. Otherwise a
        # command flood can be stopped before the flood detector sees it.
        app.add_handler(MessageHandler(filters.ALL, anti_flood_message), group=-120)
        # Delete-command moderation must run before every command handler/guard.
        # This also ensures /start, /help, /version, /admin, /connectgroup and
        # /connectsupport are deleted immediately when their sender's command
        # deletion rule is enabled.
        app.add_handler(MessageHandler(filters.ALL, moderate_seller_message), group=-110)
        app.add_handler(MessageHandler(filters.COMMAND, self.connected_group_command_guard), group=-100)
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, forced_join_editor_text_input), group=-99)
        app.add_handler(MessageHandler(filters.PHOTO | filters.VIDEO | filters.Document.ALL, forced_join_editor_media_input), group=-98)
        app.add_handler(CommandHandler("start",self.child_start))
        app.add_handler(CommandHandler("help",self.help_command))
        app.add_handler(CommandHandler("admin",self.admin))
        app.add_handler(CommandHandler("connectgroup",self.connect_group_command))
        app.add_handler(CommandHandler("connectforcedjoin",lambda update,context: connect_forced_join_command(self,update,context)))
        app.add_handler(CommandHandler("connectsupport",self.connect_support_command))
        app.add_handler(CommandHandler("confirm", self.seller_broadcast_confirm_command))
        app.add_handler(CommandHandler("cancel", self.seller_broadcast_cancel_command))
        app.add_handler(MessageHandler(filters.COMMAND,self.support_template_command_handler),group=9)
        app.add_handler(
            CommandHandler(
                "version",
                lambda update,context: update.effective_message.reply_text(
                    f"Runtime: {WELCOME_RUNTIME_VERSION}"
                ),
            )
        )
        app.add_handler(PreCheckoutQueryHandler(self.stars_precheckout), group=-40)
        app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, self.stars_success), group=-39)
        app.add_handler(CallbackQueryHandler(forced_join_info_callback, pattern=r"^fj_(forced_groups|toggle_feature|info:)"))
        app.add_handler(CallbackQueryHandler(forced_join_toggle_callback, pattern=r"^fj_toggle:"))
        app.add_handler(CallbackQueryHandler(forced_join_editor_callback, pattern=r"^fj_editor"))
        app.add_handler(CallbackQueryHandler(group_manager_special_callback, pattern=r"^gmsp"))
        # User callbacks include normal c_* actions, the seller current-plan button,
        # and shared-editor w_* popup/alert/rules actions. Register all of them
        # explicitly so generated buttons cannot become dead callbacks.
        app.add_handler(
            CallbackQueryHandler(
                self.child_callback,
                pattern=r"^(c_|seller_current_plan$|seller_upgrade_plan$|w_)",
            )
        )
        app.add_handler(CallbackQueryHandler(self.admin_callback,pattern=r"^(a_|ba_|gm_)"))
        app.add_handler(CallbackQueryHandler(self.support_callback,pattern=r"^support_"))
        for handler in deleting_messages_handlers():
            app.add_handler(handler,group=-7)
        for handler in content_protection_handlers():
            app.add_handler(handler,group=-7)
        for handler in subscription_guard_handlers():
            app.add_handler(handler,group=-7)
        app.add_handler(ChatJoinRequestHandler(forced_join_request), group=-35)
        app.add_handler(ChatMemberHandler(forced_join_auto_approve, ChatMemberHandler.CHAT_MEMBER), group=-34)
        app.add_handler(ChatMemberHandler(subscription_guard_chat_member, ChatMemberHandler.CHAT_MEMBER), group=-30)
        # Fallback for joins delivered as CHAT_MEMBER updates; NEW_CHAT_MEMBERS below remains the primary path.
        app.add_handler(ChatMemberHandler(group_manager_chat_member_welcome, ChatMemberHandler.CHAT_MEMBER), group=-28)
        app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, subscription_guard_new_members), group=-29)
        app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, group_manager_new_members), group=-28)
        app.add_handler(MessageHandler(filters.ALL, group_manager_protection_message), group=-21)
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, group_manager_message), group=-19)
        app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND,self.broadcast_message_handler),group=-3)
        app.add_handler(MessageHandler(filters.FORWARDED,self.forward_handler),group=-2)
        app.add_handler(MessageHandler(filters.PHOTO | filters.VIDEO | filters.ANIMATION | filters.Document.ALL, self.business_automation_media_handler), group=-4)
        app.add_handler(MessageHandler(filters.PHOTO | filters.VIDEO | filters.ANIMATION | filters.Document.ALL,self.welcome_media_handler),group=-1)
        # Dedicated seller Manual Payment QR handler. Keep it ahead of all generic media/routing handlers.
        app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, self.seller_qr_upload_handler), group=-130)
        app.add_handler(MessageHandler(filters.PHOTO,self.photo_handler),group=0)
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.business_automation_text_handler), group=-1)
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,self.text_handler))
        app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND,self.route_live_support_message),group=-8)
        if app.job_queue: app.job_queue.run_repeating(self.expiry_job,interval=60,first=30,name=f"seller_expiry_{data_owner_id}")
        return app

