def _show_login_dialog(self) -> bool:
        """
        Show a modal login dialog requesting repository URL and SVN credentials.
        Returns True on successful authentication, False if the user cancelled.
        """
        dlg = tk.Toplevel(self.root)
        dlg.title("SVN Login")
        dlg.transient(self.root)
        dlg.grab_set()
        # Give a reasonable default size but allow geometry managers to size widgets
        dlg.minsize(480, 220)
        dlg.resizable(False, False)

        # Ensure the dialog is raised and focused
        dlg.lift()
        try:
            dlg.attributes("-topmost", True)
            dlg.after(100, lambda: dlg.attributes("-topmost", False))
        except Exception:
            # attributes may not be supported in some environments; ignore silently
            pass

        # Variables
        url_var = tk.StringVar(value="")
        user_var = tk.StringVar(value="")
        pass_var = tk.StringVar(value="")
        remember_var = tk.BooleanVar(value=False)

        # Layout using grid so the controls expand predictably
        frm = ttk.Frame(dlg, padding=12)
        frm.grid(row=0, column=0, sticky="nsew")
        dlg.columnconfigure(0, weight=1)
        dlg.rowconfigure(0, weight=1)

        frm.columnconfigure(1, weight=1)

        ttk.Label(frm, text="Repository URL:").grid(row=0, column=0, sticky=tk.W, pady=(0, 6))
        url_entry = ttk.Entry(frm, textvariable=url_var, width=60)
        url_entry.grid(row=0, column=1, pady=(0, 6), sticky="ew")

        ttk.Label(frm, text="Username:").grid(row=1, column=0, sticky=tk.W, pady=(0, 6))
        user_entry = ttk.Entry(frm, textvariable=user_var, width=60)
        user_entry.grid(row=1, column=1, pady=(0, 6), sticky="ew")

        ttk.Label(frm, text="Password:").grid(row=2, column=0, sticky=tk.W, pady=(0, 6))
        pass_entry = ttk.Entry(frm, textvariable=pass_var, width=60, show="*")
        pass_entry.grid(row=2, column=1, pady=(0, 6), sticky="ew")

        ttk.Checkbutton(frm, text="Remember credentials (macOS Keychain)",
                        variable=remember_var).grid(row=3, column=1, sticky=tk.W, pady=(0, 6))

        status_label = ttk.Label(frm, text="", foreground="red")
        status_label.grid(row=4, column=0, columnspan=2, sticky=tk.W, pady=(6, 0))

        # Button frame placed in its own grid row so it will not be clipped
        btn_frame = ttk.Frame(frm)
        btn_frame.grid(row=5, column=0, columnspan=2, sticky="e", pady=(12, 0))
        # Give the button frame some internal padding so buttons are not flush to edge
        btn_frame.columnconfigure(0, weight=1)

        def on_ok():
            url = url_var.get().strip()
            username = user_var.get().strip()
            password = pass_var.get()
            if not url:
                messagebox.showerror(self.i18n["error"], "Repository URL is required", parent=dlg)
                return

            # Show busy cursor
            dlg.config(cursor="watch")
            dlg.update_idletasks()

            try:
                success, message = self._attempt_login(url, username, password)
            finally:
                dlg.config(cursor="")
                dlg.update_idletasks()

            if success:
                # Optionally store credentials in keychain
                if remember_var.get() and username and password:
                    try:
                        service = url
                        self.svn.credential_store.set_credential(service, username, password)
                    except Exception:
                        logging.debug("Failed to store credentials in keychain")

                try:
                    dlg.grab_release()
                except Exception:
                    pass
                dlg.destroy()
                self.status_var.set(f"Connected to {url}")
            else:
                status_label.config(text=message or "Authentication failed")
                messagebox.showerror(self.i18n["error"], message or "Failed to connect or authenticate", parent=dlg)

        def on_cancel():
            if messagebox.askyesno("Cancel", "Cancel login and quit application?", parent=dlg):
                try:
                    dlg.grab_release()
                except Exception:
                    pass
                dlg.destroy()

        ok_btn = ttk.Button(btn_frame, text="Connect", command=on_ok)
        ok_btn.pack(side=tk.RIGHT, padx=6)
        cancel_btn = ttk.Button(btn_frame, text="Cancel", command=on_cancel)
        cancel_btn.pack(side=tk.RIGHT)

        # Set focus and ensure visibility before waiting
        url_entry.focus_set()
        dlg.update_idletasks()
        dlg.wait_visibility()
        dlg.focus_force()

        # Wait for dialog to be dismissed; after it's destroyed check if we have a connection
        self.root.wait_window(dlg)

        # Determine if login succeeded by checking status_var (set on success)
        return bool(self.status_var.get().startswith("Connected to"))