# Streaming message helper functions - to be added to casino v5 (1).py

# Add after line 3851 (before the @handle_errors for start command)

# â"€â"€ STREAMING MESSAGE HELPER â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
async def send_streaming_message(update: Update, text: str, **kwargs):
    """
    Send message with optional streaming effect.
    If streaming_enabled is True, sends message in 3-5 word chunks with 150ms delays.
    Otherwise sends full message at once.
    """
    global streaming_enabled
    
    if not update.message:
        return
    
    if not streaming_enabled:
        # Normal mode: send full message
        await update.message.reply_html(text, **kwargs)
        return
    
    # Streaming mode: send in chunks
    words = text.split()
    chunk_size_min = 3
    chunk_size_max = 5
    delay_ms = 150
    
    import random
    messages = []
    i = 0
    
    while i < len(words):
        # Random chunk size between 3-5 words
        chunk_size = random.randint(chunk_size_min, min(chunk_size_max, len(words) - i))
        chunk_words = words[i:i + chunk_size]
        chunk_text = " ".join(chunk_words)
        messages.append(chunk_text)
        i += chunk_size
    
    # Send chunks progressively
    for idx, chunk in enumerate(messages):
        try:
            await update.message.reply_html(chunk)
            # Add delay between chunks (except after last)
            if idx < len(messages) - 1:
                await asyncio.sleep(delay_ms / 1000.0)
        except Exception as e:
            logger.error(f"Error sending streaming chunk: {e}")
            # Fallback: send remaining text as one message
            remaining = " ".join(messages[idx:])
            await update.message.reply_html(remaining, **kwargs)
            return


async def stream_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Enable streaming message effect - admin only"""
    global streaming_enabled
    
    if not update.message:
        return
    
    user_id = update.effective_user.id
    
    if not is_admin(user_id):
        await update.message.reply_html(
            translate_text("âŒ <b>You don't have permission to use this command.</b>", user_id=user_id)
        )
        return
    
    streaming_enabled = True
    await update.message.reply_html(
        "✅ <b>Streaming enabled!</b>\n\n"
        "Bot will now send all messages in 3-5 word chunks with 150ms delays.\n"
        "Use /streamoff to disable."
    )


async def streamoff_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Disable streaming message effect - admin only"""
    global streaming_enabled
    
    if not update.message:
        return
    
    user_id = update.effective_user.id
    
    if not is_admin(user_id):
        await update.message.reply_html(
            translate_text("âŒ <b>You don't have permission to use this command.</b>", user_id=user_id)
        )
        return
    
    streaming_enabled = False
    await update.message.reply_html(
        "✅ <b>Streaming disabled!</b>\n\n"
        "Bot will send normal messages.\n"
        "Use /stream to enable."
    )

