#!/usr/bin/env python
# Insert streaming functions into casino v5 (1).py

with open('casino v5 (1).py', 'r', encoding='utf-8') as f:
    content = f.read()

# The streaming command functions to insert
streaming_code = '''

async def stream_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Enable streaming message effect - admin only"""
    global streaming_enabled
    if not update.message:
        return
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_html(translate_text("âŒ Permission denied", user_id=user_id))
        return
    streaming_enabled = True
    await update.message.reply_html("✅ <b>Streaming ENABLED</b>\\n3-5 word chunks, 150ms delays\\nUse /streamoff to disable")


async def streamoff_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Disable streaming message effect - admin only"""
    global streaming_enabled
    if not update.message:
        return
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_html(translate_text("âŒ Permission denied", user_id=user_id))
        return
    streaming_enabled = False
    await update.message.reply_html("✅ <b>Streaming DISABLED</b> - Normal messages\\nUse /stream to enable")

'''

# Find insertion point - after eventstatus_command  
marker = 'await update.message.reply_html("\\n".join(lines))'
if marker in content:
    # Find this marker in the eventstatus_command context (should be around line 11793)
    parts = content.split(marker)
    
    # Take first 2 occurrences and insert after the second one
    if len(parts) >= 3:
        # Reconstruct with insertion after eventstatus
        new_content = parts[0] + marker + streaming_code + marker.join(parts[1:])
    else:
        # Single occurrence - insert after it
        new_content = content.replace(marker, marker + streaming_code, 1)
    
    with open('casino v5 (1).py', 'w', encoding='utf-8') as f:
        f.write(new_content)
    print("Streaming functions inserted successfully!")
else:
    print("Could not find insertion marker in file")
