package com.littletoby.app;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.graphics.drawable.GradientDrawable;
import android.net.Uri;
import android.os.Bundle;
import android.text.InputType;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Little Toby for Android.
 *
 * The computer already serves the phone app (the same one you'd open in a
 * browser), so this is a home for it: the first time, you paste the pairing
 * link that `toby phone on` shows (or type the address and code), and from
 * then on the app opens straight to your computer. Everything goes over your
 * own Tailscale network, like the iPhone app.
 */
public class MainActivity extends Activity {
    private static final int BG = Color.parseColor("#0b0c17");
    private static final int CARD = Color.parseColor("#1b1d44");
    private static final int ACCENT = Color.parseColor("#ffa626");
    private static final int TEXT = Color.parseColor("#f4f4fb");
    private static final int MUTED = Color.parseColor("#a9abc9");

    private static final Pattern URL = Pattern.compile("https?://\\S+", Pattern.CASE_INSENSITIVE);
    private static final Pattern CODE = Pattern.compile("\\b([A-Za-z0-9]{4})[- ]?([A-Za-z0-9]{4})\\b");

    private SharedPreferences prefs;
    private WebView web;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        prefs = getSharedPreferences("toby", MODE_PRIVATE);
        String shared = sharedText(getIntent());
        String base = prefs.getString("base", null);
        if (shared != null) {
            connect(shared, null);
        } else if (base != null) {
            showWeb(base);
        } else {
            showSetup(null, null);
        }
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        String shared = sharedText(intent);
        if (shared != null) connect(shared, null);
    }

    private static String sharedText(Intent intent) {
        if (intent == null || !Intent.ACTION_SEND.equals(intent.getAction())) return null;
        return intent.getStringExtra(Intent.EXTRA_TEXT);
    }

    // ------------------------------------------------------------------
    // Pairing link → address to open
    // ------------------------------------------------------------------

    /** Returns {base, urlToOpen}, or null if there's no address in it. */
    static String[] parse(String input) {
        String text = input == null ? "" : input.trim();
        if (text.isEmpty()) return null;
        String url;
        Matcher m = URL.matcher(text);
        String rest;
        if (m.find()) {
            url = m.group();
            rest = text.substring(0, m.start()) + " " + text.substring(m.end());
        } else {
            String[] words = text.split("\\s+");
            if (!words[0].contains(".")) return null;
            url = "https://" + words[0];
            rest = text.substring(words[0].length());
        }
        Uri uri = Uri.parse(url);
        if (uri.getHost() == null || uri.getHost().isEmpty()) return null;
        String base = uri.getScheme() + "://" + uri.getHost() + (uri.getPort() > 0 ? ":" + uri.getPort() : "") + "/";
        String fragment = uri.getFragment();
        if (fragment != null && fragment.contains("pair=")) {
            return new String[] {base, base + "#" + fragment};
        }
        Matcher c = CODE.matcher(rest);
        if (c.find()) {
            return new String[] {base, base + "#pair=" + (c.group(1) + c.group(2)).toUpperCase()};
        }
        return new String[] {base, base};
    }

    private void connect(String input, TextView status) {
        String[] parsed = parse(input);
        if (parsed == null) {
            if (status != null) status.setText("That doesn't look like the link from `toby phone on`. It starts with https://");
            else showSetup(input, "That doesn't look like the link from `toby phone on`.");
            return;
        }
        prefs.edit().putString("base", parsed[0]).apply();
        showWeb(parsed[1]);
    }

    // ------------------------------------------------------------------
    // Screens
    // ------------------------------------------------------------------

    private int dp(int v) {
        return (int) TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, v, getResources().getDisplayMetrics());
    }

    private TextView text(String s, int sp, int color) {
        TextView t = new TextView(this);
        t.setText(s);
        t.setTextSize(TypedValue.COMPLEX_UNIT_SP, sp);
        t.setTextColor(color);
        t.setLineSpacing(0, 1.15f);
        return t;
    }

    private void showSetup(String prefill, String message) {
        if (web != null) {
            web.destroy();
            web = null;
        }
        LinearLayout col = new LinearLayout(this);
        col.setOrientation(LinearLayout.VERTICAL);
        col.setPadding(dp(24), dp(56), dp(24), dp(32));

        TextView title = text("Little Toby", 30, TEXT);
        title.setTypeface(title.getTypeface(), android.graphics.Typeface.BOLD);
        col.addView(title);

        TextView lead = text("Talk to Toby on your computer, from anywhere.", 17, MUTED);
        lead.setPadding(0, dp(6), 0, dp(24));
        col.addView(lead);

        String steps = "1.  On the computer, run:  toby phone on\n"
                + "2.  Make sure Tailscale is on, on this phone.\n"
                + "3.  Paste the link it shows below, or type the address and the code.";
        TextView how = text(steps, 15, TEXT);
        how.setPadding(dp(16), dp(16), dp(16), dp(16));
        GradientDrawable card = new GradientDrawable();
        card.setColor(CARD);
        card.setCornerRadius(dp(16));
        how.setBackground(card);
        col.addView(how);

        EditText field = new EditText(this);
        field.setHint("https://your-computer.ts.net   D643-HZ8E");
        field.setHintTextColor(MUTED);
        field.setTextColor(TEXT);
        field.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        field.setSingleLine(false);
        if (prefill != null) field.setText(prefill);
        LinearLayout.LayoutParams fp = new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        fp.topMargin = dp(20);
        col.addView(field, fp);

        TextView status = text(message == null ? "" : message, 14, ACCENT);
        status.setPadding(0, dp(8), 0, 0);
        col.addView(status);

        Button go = new Button(this);
        go.setText("Connect");
        go.setAllCaps(false);
        go.setTextColor(BG);
        go.setTextSize(TypedValue.COMPLEX_UNIT_SP, 17);
        GradientDrawable pill = new GradientDrawable();
        pill.setColor(ACCENT);
        pill.setCornerRadius(dp(28));
        go.setBackground(pill);
        LinearLayout.LayoutParams gp = new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(56));
        gp.topMargin = dp(16);
        col.addView(go, gp);
        go.setOnClickListener(v -> connect(field.getText().toString(), status));

        final String saved = prefs.getString("base", null);
        if (saved != null) {
            Button again = new Button(this);
            again.setText("Try " + Uri.parse(saved).getHost() + " again");
            again.setAllCaps(false);
            again.setTextColor(TEXT);
            again.setBackgroundColor(Color.TRANSPARENT);
            col.addView(again, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(52)));
            again.setOnClickListener(v -> showWeb(saved));
        }

        TextView help = text("New to Little Toby? Install it on your Linux computer first: "
                + "mr1charles.github.io/linux-ai-assistant-", 13, MUTED);
        help.setPadding(0, dp(28), 0, 0);
        help.setGravity(Gravity.CENTER_HORIZONTAL);
        help.setOnClickListener(v -> openOutside(Uri.parse("https://mr1charles.github.io/linux-ai-assistant-/")));
        col.addView(help);

        ScrollView scroll = new ScrollView(this);
        scroll.setFillViewport(true);
        scroll.setBackgroundColor(BG);
        scroll.addView(col);
        setContentView(scroll);
    }

    private void showWeb(String url) {
        if (web == null) {
            web = new WebView(this);
            web.setBackgroundColor(BG);
            WebSettings s = web.getSettings();
            s.setJavaScriptEnabled(true);
            s.setDomStorageEnabled(true);
            s.setDatabaseEnabled(true);
            s.setMediaPlaybackRequiresUserGesture(false);
            web.setWebViewClient(new WebViewClient() {
                @Override
                public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                    Uri target = request.getUrl();
                    String base = prefs.getString("base", "");
                    if (target.getHost() != null && target.getHost().equals(Uri.parse(base).getHost())) return false;
                    openOutside(target);
                    return true;
                }

                @Override
                public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
                    if (request.isForMainFrame()) {
                        showSetup(null, "Couldn't reach your computer. Check that Tailscale is on here, "
                                + "and that the computer is awake with Toby running.");
                    }
                }
            });
            setContentView(web);
        }
        web.loadUrl(url);
    }

    private void openOutside(Uri uri) {
        try {
            startActivity(new Intent(Intent.ACTION_VIEW, uri));
        } catch (Exception ignored) {
        }
    }

    @Override
    public void onBackPressed() {
        if (web != null && web.canGoBack()) {
            web.goBack();
        } else if (web != null) {
            new AlertDialog.Builder(this)
                    .setItems(new CharSequence[] {"Close Little Toby", "Connect to a different computer"}, (d, which) -> {
                        if (which == 0) finish();
                        else showSetup(null, null);
                    })
                    .show();
        } else {
            super.onBackPressed();
        }
    }
}
