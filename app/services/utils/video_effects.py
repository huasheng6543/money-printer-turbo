from moviepy.video import fx as vfx


# FadeIn
def fadein_transition(clip, t):
    return clip.with_effects([vfx.FadeIn(t)])


# FadeOut
def fadeout_transition(clip, t):
    return clip.with_effects([vfx.FadeOut(t)])


# SlideIn
def slidein_transition(clip, t, side):
    return clip.with_effects([vfx.SlideIn(t, side)])


# SlideOut
def slideout_transition(clip, t, side):
    return clip.with_effects([vfx.SlideOut(t, side)])
