package com.wpsteuerung.app.ui.theme

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Shapes
import androidx.compose.material3.Typography
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.Font
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.foundation.shape.RoundedCornerShape
import com.wpsteuerung.app.R

// ─────────────────────────────────────────────
// Farbpalette — "Industrial Thermal"
// Warme Amber-Töne für Wärmeenergie, kühles Blau als Kontrast
// ─────────────────────────────────────────────

private val Amber700   = Color(0xFFF57F17)
private val Amber500   = Color(0xFFFFB300)
private val Amber200   = Color(0xFFFFE082)
private val Amber50    = Color(0xFFFFF8E1)

private val DeepBlue   = Color(0xFF0D1B2A)
private val SteelBlue  = Color(0xFF1E3A5F)
private val SlateGray  = Color(0xFF37474F)

private val LightColors = lightColorScheme(
    primary            = Color(0xFFB45309),   // Amber-Braun
    onPrimary          = Color(0xFFFFFFFF),
    primaryContainer   = Color(0xFFFFECB3),
    onPrimaryContainer = Color(0xFF3E1C00),
    secondary          = Color(0xFF1565C0),   // Stahlblau
    onSecondary        = Color(0xFFFFFFFF),
    secondaryContainer = Color(0xFFD6E4FF),
    onSecondaryContainer = Color(0xFF001849),
    tertiary           = Color(0xFF2E7D32),
    onTertiary         = Color(0xFFFFFFFF),
    tertiaryContainer  = Color(0xFFC8E6C9),
    onTertiaryContainer= Color(0xFF002106),
    error              = Color(0xFFBA1A1A),
    errorContainer     = Color(0xFFFFDAD6),
    onError            = Color(0xFFFFFFFF),
    onErrorContainer   = Color(0xFF410002),
    background         = Color(0xFFFAF7F2),   // Warmes Off-White
    onBackground       = Color(0xFF1A1410),
    surface            = Color(0xFFFFFFFF),
    onSurface          = Color(0xFF1A1410),
    surfaceVariant     = Color(0xFFF3EDE3),
    onSurfaceVariant   = Color(0xFF4E453A),
    outline            = Color(0xFF7F7367),
)

private val DarkColors = darkColorScheme(
    primary            = Amber500,
    onPrimary          = Color(0xFF3E1C00),
    primaryContainer   = Color(0xFF7A3D00),
    onPrimaryContainer = Amber200,
    secondary          = Color(0xFF90CAF9),
    onSecondary        = Color(0xFF003064),
    secondaryContainer = SteelBlue,
    onSecondaryContainer = Color(0xFFD6E4FF),
    tertiary           = Color(0xFFA5D6A7),
    onTertiary         = Color(0xFF003910),
    tertiaryContainer  = Color(0xFF1B5E20),
    onTertiaryContainer= Color(0xFFC8E6C9),
    error              = Color(0xFFFFB4AB),
    errorContainer     = Color(0xFF93000A),
    onError            = Color(0xFF690005),
    onErrorContainer   = Color(0xFFFFDAD6),
    background         = DeepBlue,
    onBackground       = Color(0xFFEDE8E0),
    surface            = Color(0xFF112030),
    onSurface          = Color(0xFFEDE8E0),
    surfaceVariant     = Color(0xFF1E2D3D),
    onSurfaceVariant   = Color(0xFFBDB5AA),
    outline            = Color(0xFF6B6057),
)

// ─────────────────────────────────────────────
// Typografie — Exo 2 (technisch) + Nunito (lesbar)
// HINWEIS: Füge in build.gradle hinzu:
//   implementation("androidx.compose.ui:ui-text-google-fonts:1.7.8")
// Und lege res/font/exo2_*.ttf + nunito_*.ttf an, oder nutze GoogleFont:
// ─────────────────────────────────────────────

val AppTypography = Typography(
    displayLarge = TextStyle(
        fontWeight = FontWeight.Bold,
        fontSize = 57.sp,
        letterSpacing = (-0.25).sp
    ),
    headlineLarge = TextStyle(
        fontWeight = FontWeight.Bold,
        fontSize = 32.sp,
        letterSpacing = 0.sp
    ),
    headlineMedium = TextStyle(
        fontWeight = FontWeight.SemiBold,
        fontSize = 24.sp,
        letterSpacing = 0.sp
    ),
    headlineSmall = TextStyle(
        fontWeight = FontWeight.SemiBold,
        fontSize = 20.sp,
        letterSpacing = 0.sp
    ),
    titleLarge = TextStyle(
        fontWeight = FontWeight.SemiBold,
        fontSize = 18.sp,
        letterSpacing = 0.sp
    ),
    titleMedium = TextStyle(
        fontWeight = FontWeight.Medium,
        fontSize = 16.sp,
        letterSpacing = 0.15.sp
    ),
    bodyLarge = TextStyle(
        fontWeight = FontWeight.Normal,
        fontSize = 16.sp,
        letterSpacing = 0.5.sp
    ),
    bodyMedium = TextStyle(
        fontWeight = FontWeight.Normal,
        fontSize = 14.sp,
        letterSpacing = 0.25.sp
    ),
    labelLarge = TextStyle(
        fontWeight = FontWeight.Medium,
        fontSize = 14.sp,
        letterSpacing = 0.1.sp
    ),
    labelMedium = TextStyle(
        fontWeight = FontWeight.Medium,
        fontSize = 12.sp,
        letterSpacing = 0.5.sp
    ),
    labelSmall = TextStyle(
        fontWeight = FontWeight.Medium,
        fontSize = 11.sp,
        letterSpacing = 0.5.sp
    )
)

// ─────────────────────────────────────────────
// Shapes — leicht abgerundeter industrieller Look
// ─────────────────────────────────────────────

val AppShapes = Shapes(
    extraSmall = RoundedCornerShape(4.dp),
    small      = RoundedCornerShape(8.dp),
    medium     = RoundedCornerShape(12.dp),
    large      = RoundedCornerShape(16.dp),
    extraLarge = RoundedCornerShape(24.dp)
)

// ─────────────────────────────────────────────
// Theme Entry Point
// ─────────────────────────────────────────────

@Composable
fun WPSteuerungTheme(
    darkTheme: Boolean = isSystemInDarkTheme(), // FIX: folgt jetzt der Systemeinstellung
    content: @Composable () -> Unit
) {
    val colorScheme = if (darkTheme) DarkColors else LightColors

    MaterialTheme(
        colorScheme = colorScheme,
        typography  = AppTypography,
        shapes      = AppShapes,
        content     = content
    )
}