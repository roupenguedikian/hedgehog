The bots main engine needs to evaluate every single active symbol during every cycle. I think (just brainstorming) that the engine should "tag" or "assign" each symbol to a path based on a few things. The engine starts by ingesting every symbols funding rates from each exchange. it maps out the best short funding rate (bsfr) & best long funding rate (blfr) for each symbol. from there is calculates each symbols best net dpy (bndpy) and best breakeven hours (bbh). It starts with an analysis of current open positions


##Variables
clfr= current long funding rate
csfr= current short funding rate
blfr= best long funding rate
bsfr= best short funding rate
cndpy= current ndpy
bndpy= best possible ndpy (per symbol)
endpy= entry ndpy minimum
xndpy= exit ndpy maximum
mebh= maximum entry breakeven hours 
cebh= current entry breakeven hours
mrbh= maximum rotation breakeven hours
crbh= current rotation breakeven hours


#Engine
1. open position? if yes go to 2; if no go to 6
2. is symbol hedged? if no go to 3; if yes go to 4
3. is bndpy < endpy? if yes then tag exit; if no tag hedge
4. if cndpy < xndpy -> check bndpy 
    4a. bndpy > endpy = tag rotation 
    4b. bndpy < endpy = tag exit? idk
5. if cndpy > xndpy -> check bndpy
    5a. if cndpy = bndpy tag hold
    5b. if cndpy < bndpy check rotation breakeven hours
    5c. if crbh < mrbh = tag rotation; if crbh > mrbh = tag hold
 6. bndpy < endpy = no entry tag; bndpy > endpy = entry tag (3x entry tag in a row = entry execution)





-well yes if any symbol is unhedged its a failure state. the right logic is to check whether its a better idea to
exit or attempt to hedge again at same exchange or 2nd best option if yield is appropriate. thoughts?

-completely agree with this "4a. bndpy > endpy AND crbh < mrbh → tag rotation
4a. bndpy > endpy AND crbh > mrbh → tag exit (cheaper to exit and re-enter fresh)"


-so global rule would be that if a position doesnt meet threshold it loses all its tags. only positions with x tags are eligible 
for execution. we can set that x as an env var default = 3 to start

-theres some more nuance when it comes to rotation and sizing. first of all when rotation occurs, i would prefer that only 1 leg rotates (the weaker one). then if another rotation is triggered the other leg can rotate. but as far as sizing it basically needs to predict its future ndpy (if both legs arent rotating) so that it can size correctly. like what if bdpy is such that it only makes sense for one leg to rotate because the other legs breakeven hour is too high. anyways just spitballing here.

-agree with max allocation cap based on NAV (20% makes sense); but what about leverage? do we cap that at 10x? 5x?

-lets keep entries simple for now. sort by NDPY and 1 entry per cycle. but ive also been in situations where the bot get stuck because it cant execute entry #1 and never skips it to go for entry #2. so i dont know what the best move is here. open to suggestions

-agree with this "base_size = configurable minimum (e.g. $200)
max_size_for_group = base_size × (group_bndpy / endpy)

but capped by:
  - max_single_position_pct × total_nav (e.g. 20%)
  - available capital across both venues
  - venue-level exposure limit"