package main

import "testing"

func TestEveryNativeCommandIsDescribedInBothLanguages(t *testing.T) {
	for _, lang := range []string{"en", "zh"} {
		info := commandInfoList(lang)
		names := nativeCommands()
		if len(info) != len(names) {
			t.Fatalf("%s: %d described for %d commands", lang, len(info), len(names))
		}
		for i, row := range info {
			if row["name"] != names[i] || row["description"] == "" {
				t.Fatalf("%s: %q has no description", lang, names[i])
			}
		}
	}
	if commandInfoList("zh")[0]["description"] == commandInfoList("en")[0]["description"] {
		t.Fatal("Chinese descriptions should differ from English")
	}
}
